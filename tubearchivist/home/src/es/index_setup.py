"""
functionality:
- setup elastic index at first start
- verify and update index mapping and settings if needed
- backup and restore metadata
"""

import json
import os
import zipfile
from datetime import datetime

import requests
from home.src.ta.config import AppConfig
from home.src.ta.helper import ignore_filelist


class ElasticIndex:
    """
    handle mapping and settings on elastic search for a given index
    """

    CONFIG = AppConfig().config
    ES_URL = CONFIG["application"]["es_url"]
    ES_AUTH = CONFIG["application"]["es_auth"]
    HEADERS = {"Content-type": "application/json"}

    def __init__(self, index_name, expected_map, expected_set):
        self.index_name = index_name
        self.expected_map = expected_map
        self.expected_set = expected_set
        self.exists, self.details = self.index_exists()

    def index_exists(self):
        """check if index already exists and return mapping if it does"""
        index_name = self.index_name
        url = f"{self.ES_URL}/ta_{index_name}"
        response = requests.get(url, auth=self.ES_AUTH)
        exists = response.ok

        if exists:
            details = response.json()[f"ta_{index_name}"]
        else:
            details = False

        return exists, details

    def validate(self):
        """
        check if all expected mappings and settings match
        returns True when rebuild is needed
        """

        if self.expected_map:
            rebuild = self.validate_mappings()
            if rebuild:
                return rebuild

        if self.expected_set:
            rebuild = self.validate_settings()
            if rebuild:
                return rebuild

        return False

    def validate_mappings(self):
        """check if all mappings are as expected"""

        expected_map = self.expected_map
        now_map = self.details["mappings"]["properties"]

        for key, value in expected_map.items():
            # nested
            if list(value.keys()) == ["properties"]:
                for key_n, value_n in value["properties"].items():
                    if key_n not in now_map[key]["properties"].keys():
                        print(key_n, value_n)
                        return True
                    if not value_n == now_map[key]["properties"][key_n]:
                        print(key_n, value_n)
                        return True

                continue

            # not nested
            if key not in now_map.keys():
                print(key, value)
                return True
            if not value == now_map[key]:
                print(key, value)
                return True

        return False

    def validate_settings(self):
        """check if all settings are as expected"""

        now_set = self.details["settings"]["index"]

        for key, value in self.expected_set.items():
            if key not in now_set.keys():
                print(key, value)
                return True

            if not value == now_set[key]:
                print(key, value)
                return True

        return False

    def rebuild_index(self):
        """rebuild with new mapping"""
        # backup
        self.reindex("backup")
        # delete original
        self.delete_index(backup=False)
        # create new
        self.create_blank()
        self.reindex("restore")
        # delete backup
        self.delete_index()

    def reindex(self, method):
        """create on elastic search"""
        index_name = self.index_name
        if method == "backup":
            source = f"ta_{index_name}"
            destination = f"ta_{index_name}_backup"
        elif method == "restore":
            source = f"ta_{index_name}_backup"
            destination = f"ta_{index_name}"

        query = {"source": {"index": source}, "dest": {"index": destination}}
        data = json.dumps(query)
        url = self.ES_URL + "/_reindex?refresh=true"
        response = requests.post(
            url=url, data=data, headers=self.HEADERS, auth=self.ES_AUTH
        )
        if not response.ok:
            print(response.text)

    def delete_index(self, backup=True):
        """delete index passed as argument"""
        if backup:
            url = f"{self.ES_URL}/ta_{self.index_name}_backup"
        else:
            url = f"{self.ES_URL}/ta_{self.index_name}"
        response = requests.delete(url, auth=self.ES_AUTH)
        if not response.ok:
            print(response.text)

    def create_blank(self):
        """apply new mapping and settings for blank new index"""
        expected_map = self.expected_map
        expected_set = self.expected_set
        # stich payload
        payload = {}
        if expected_set:
            payload.update({"settings": expected_set})
        if expected_map:
            payload.update({"mappings": {"properties": expected_map}})
        # create
        url = f"{self.ES_URL}/ta_{self.index_name}"
        data = json.dumps(payload)
        response = requests.put(
            url=url, data=data, headers=self.HEADERS, auth=self.ES_AUTH
        )
        if not response.ok:
            print(response.text)


class ElasticBackup:
    """dump index to nd-json files for later bulk import"""

    def __init__(self, index_config, reason):
        self.config = AppConfig().config
        self.index_config = index_config
        self.reason = reason
        self.timestamp = datetime.now().strftime("%Y%m%d")
        self.backup_files = []

    def get_all_documents(self, index_name):
        """export all documents of a single index"""
        headers = {"Content-type": "application/json"}
        es_url = self.config["application"]["es_url"]
        es_auth = self.config["application"]["es_auth"]
        # get PIT ID
        url = f"{es_url}/ta_{index_name}/_pit?keep_alive=1m"
        response = requests.post(url, auth=es_auth)
        json_data = json.loads(response.text)
        pit_id = json_data["id"]
        # build query
        data = {
            "query": {"match_all": {}},
            "size": 100,
            "pit": {"id": pit_id, "keep_alive": "1m"},
            "sort": [{"_id": {"order": "asc"}}],
        }
        query_str = json.dumps(data)
        url = es_url + "/_search"
        # loop until nothing left
        all_results = []
        while True:
            response = requests.get(
                url, data=query_str, headers=headers, auth=es_auth
            )
            json_data = json.loads(response.text)
            all_hits = json_data["hits"]["hits"]
            if all_hits:
                for hit in all_hits:
                    search_after = hit["sort"]
                    all_results.append(hit)
                # update search_after with last hit data
                data["search_after"] = search_after
                query_str = json.dumps(data)
            else:
                break
        # clean up PIT
        query_str = json.dumps({"id": pit_id})
        requests.delete(
            es_url + "/_pit", data=query_str, headers=headers, auth=es_auth
        )

        return all_results

    @staticmethod
    def build_bulk(all_results):
        """build bulk query data from all_results"""
        bulk_list = []

        for document in all_results:
            document_id = document["_id"]
            es_index = document["_index"]
            action = {"index": {"_index": es_index, "_id": document_id}}
            source = document["_source"]
            bulk_list.append(json.dumps(action))
            bulk_list.append(json.dumps(source))

        # add last newline
        bulk_list.append("\n")
        file_content = "\n".join(bulk_list)

        return file_content

    def write_es_json(self, file_content, index_name):
        """write nd-json file for es _bulk API to disk"""
        cache_dir = self.config["application"]["cache_dir"]
        file_name = f"es_{index_name}-{self.timestamp}.json"
        file_path = os.path.join(cache_dir, "backup", file_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        self.backup_files.append(file_path)

    def write_ta_json(self, all_results, index_name):
        """write generic json file to disk"""
        cache_dir = self.config["application"]["cache_dir"]
        file_name = f"ta_{index_name}-{self.timestamp}.json"
        file_path = os.path.join(cache_dir, "backup", file_name)
        to_write = [i["_source"] for i in all_results]
        file_content = json.dumps(to_write)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        self.backup_files.append(file_path)

    def zip_it(self):
        """pack it up into single zip file"""
        cache_dir = self.config["application"]["cache_dir"]
        file_name = f"ta_backup-{self.timestamp}-{self.reason}.zip"
        backup_folder = os.path.join(cache_dir, "backup")
        backup_file = os.path.join(backup_folder, file_name)

        with zipfile.ZipFile(
            backup_file, "w", compression=zipfile.ZIP_DEFLATED
        ) as zip_f:
            for backup_file in self.backup_files:
                zip_f.write(backup_file, os.path.basename(backup_file))

        # cleanup
        for backup_file in self.backup_files:
            os.remove(backup_file)

    def post_bulk_restore(self, file_name):
        """send bulk to es"""
        cache_dir = self.config["application"]["cache_dir"]
        es_url = self.config["application"]["es_url"]
        es_auth = self.config["application"]["es_auth"]
        headers = {"Content-type": "application/x-ndjson"}
        file_path = os.path.join(cache_dir, file_name)

        with open(file_path, "r", encoding="utf-8") as f:
            query_str = f.read()

        if not query_str.strip():
            return

        url = es_url + "/_bulk"
        request = requests.post(
            url, data=query_str, headers=headers, auth=es_auth
        )
        if not request.ok:
            print(request.text)

    def get_all_backup_files(self):
        """build all available backup files for view"""
        cache_dir = self.config["application"]["cache_dir"]
        backup_dir = os.path.join(cache_dir, "backup")
        backup_files = os.listdir(backup_dir)
        all_backup_files = ignore_filelist(backup_files)
        all_available_backups = [
            i
            for i in all_backup_files
            if i.startswith("ta_") and i.endswith(".zip")
        ]
        all_available_backups.sort(reverse=True)

        backup_dicts = []
        for backup_file in all_available_backups:
            file_split = backup_file.split("-")
            if len(file_split) == 2:
                timestamp = file_split[1].strip(".zip")
                reason = False
            elif len(file_split) == 3:
                timestamp = file_split[1]
                reason = file_split[2].strip(".zip")

            to_add = {
                "filename": backup_file,
                "timestamp": timestamp,
                "reason": reason,
            }
            backup_dicts.append(to_add)

        return backup_dicts

    def unpack_zip_backup(self, filename):
        """extract backup zip and return filelist"""
        cache_dir = self.config["application"]["cache_dir"]
        backup_dir = os.path.join(cache_dir, "backup")
        file_path = os.path.join(backup_dir, filename)

        with zipfile.ZipFile(file_path, "r") as z:
            zip_content = z.namelist()
            z.extractall(backup_dir)

        return zip_content

    def restore_json_files(self, zip_content):
        """go through the unpacked files and restore"""

        cache_dir = self.config["application"]["cache_dir"]
        backup_dir = os.path.join(cache_dir, "backup")

        for json_f in zip_content:

            file_name = os.path.join(backup_dir, json_f)

            if not json_f.startswith("es_") or not json_f.endswith(".json"):
                os.remove(file_name)
                continue

            print("restoring: " + json_f)
            self.post_bulk_restore(file_name)
            os.remove(file_name)

    def index_exists(self, index_name):
        """check if index already exists to skip"""
        es_url = self.config["application"]["es_url"]
        es_auth = self.config["application"]["es_auth"]
        url = f"{es_url}/ta_{index_name}"
        response = requests.get(url, auth=es_auth)

        return response.ok

    def rotate_backup(self):
        """delete old backups if needed"""
        rotate = self.config["scheduler"]["run_backup_rotate"]
        if not rotate:
            return

        all_backup_files = self.get_all_backup_files()
        auto = [i for i in all_backup_files if i["reason"] == "auto"]

        if len(auto) <= rotate:
            print("no backup files to rotate")
            return

        cache_dir = self.config["application"]["cache_dir"]
        backup_dir = os.path.join(cache_dir, "backup")

        all_to_delete = auto[rotate:]
        for to_delete in all_to_delete:
            file_path = os.path.join(backup_dir, to_delete["filename"])
            print(f"remove old backup file: {file_path}")
            os.remove(file_path)


def get_mapping():
    """read index_mapping.json and get expected mapping and settings"""
    with open("home/src/es/index_mapping.json", "r", encoding="utf-8") as f:
        index_config = json.load(f).get("index_config")

    return index_config


def index_check(force_restore=False):
    """check if all indexes are created and have correct mapping"""

    backed_up = False
    index_config = get_mapping()

    for index in index_config:
        index_name = index["index_name"]
        expected_map = index["expected_map"]
        expected_set = index["expected_set"]
        handler = ElasticIndex(index_name, expected_map, expected_set)
        # force restore
        if force_restore:
            handler.delete_index(backup=False)
            handler.create_blank()
            continue

        # create new
        if not handler.exists:
            print(f"create new blank index with name ta_{index_name}...")
            handler.create_blank()
            continue

        # validate index
        rebuild = handler.validate()
        if rebuild:
            # make backup before rebuild
            if not backed_up:
                print("running backup first")
                backup_all_indexes(reason="update")
                backed_up = True

            print(f"applying new mappings to index ta_{index_name}...")
            handler.rebuild_index()
            continue

        # else all good
        print(f"ta_{index_name} index is created and up to date...")


def get_available_backups():
    """return dict of available backups for settings view"""
    index_config = get_mapping()
    backup_handler = ElasticBackup(index_config, reason=False)
    all_backup_files = backup_handler.get_all_backup_files()
    return all_backup_files


def backup_all_indexes(reason):
    """backup all es indexes to disk"""
    index_config = get_mapping()
    backup_handler = ElasticBackup(index_config, reason)

    for index in backup_handler.index_config:
        index_name = index["index_name"]
        if not backup_handler.index_exists(index_name):
            continue
        all_results = backup_handler.get_all_documents(index_name)
        file_content = backup_handler.build_bulk(all_results)
        backup_handler.write_es_json(file_content, index_name)
        backup_handler.write_ta_json(all_results, index_name)

    backup_handler.zip_it()

    if reason == "auto":
        backup_handler.rotate_backup()


def restore_from_backup(filename):
    """restore indexes from backup file"""
    # delete
    index_check(force_restore=True)
    # recreate
    index_config = get_mapping()
    backup_handler = ElasticBackup(index_config, reason=False)
    zip_content = backup_handler.unpack_zip_backup(filename)
    backup_handler.restore_json_files(zip_content)
