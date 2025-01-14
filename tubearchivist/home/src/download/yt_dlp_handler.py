"""
functionality:
- handle yt_dlp
- build options and post processor
- download video files
- move to archive
"""

import os
import shutil
from datetime import datetime
from time import sleep

import requests
import yt_dlp
from home.src.download.queue import PendingList
from home.src.download.subscriptions import PlaylistSubscription
from home.src.es.connect import IndexPaginate
from home.src.index.channel import YoutubeChannel
from home.src.index.playlist import YoutubePlaylist
from home.src.index.video import YoutubeVideo, index_new_video
from home.src.ta.config import AppConfig
from home.src.ta.helper import clean_string, ignore_filelist
from home.src.ta.ta_redis import RedisArchivist, RedisQueue


class VideoDownloader:
    """
    handle the video download functionality
    if not initiated with list, take from queue
    """

    def __init__(self, youtube_id_list=False):
        self.obs = False
        self.youtube_id_list = youtube_id_list
        self.config = AppConfig().config
        self._build_obs()
        self.channels = set()

    def run_queue(self):
        """setup download queue in redis loop until no more items"""
        queue = RedisQueue("dl_queue")

        limit_queue = self.config["downloads"]["limit_count"]
        if limit_queue:
            queue.trim(limit_queue - 1)

        while True:
            youtube_id = queue.get_next()
            if not youtube_id:
                break

            try:
                self._dl_single_vid(youtube_id)
            except yt_dlp.utils.DownloadError:
                print("failed to download " + youtube_id)
                continue
            vid_dict = index_new_video(youtube_id)
            self.channels.add(vid_dict["channel"]["channel_id"])
            self.move_to_archive(vid_dict)
            self._delete_from_pending(youtube_id)

        autodelete_days = self.config["downloads"]["autodelete_days"]
        if autodelete_days:
            print(f"auto delete older than {autodelete_days} days")
            self.auto_delete_watched(autodelete_days)

    @staticmethod
    def add_pending():
        """add pending videos to download queue"""
        mess_dict = {
            "status": "message:download",
            "level": "info",
            "title": "Looking for videos to download",
            "message": "Scanning your download queue.",
        }
        RedisArchivist().set_message("message:download", mess_dict)
        all_pending, _ = PendingList().get_all_pending()
        to_add = [i["youtube_id"] for i in all_pending]
        if not to_add:
            # there is nothing pending
            print("download queue is empty")
            mess_dict = {
                "status": "message:download",
                "level": "error",
                "title": "Download queue is empty",
                "message": "Add some videos to the queue first.",
            }
            RedisArchivist().set_message("message:download", mess_dict)
            return

        queue = RedisQueue("dl_queue")
        queue.add_list(to_add)

    @staticmethod
    def _progress_hook(response):
        """process the progress_hooks from yt_dlp"""
        # title
        path = os.path.split(response["filename"])[-1][12:]
        filename = os.path.splitext(os.path.splitext(path)[0])[0]
        filename_clean = filename.replace("_", " ")
        title = "Downloading: " + filename_clean
        # message
        try:
            percent = response["_percent_str"]
            size = response["_total_bytes_str"]
            speed = response["_speed_str"]
            eta = response["_eta_str"]
            message = f"{percent} of {size} at {speed} - time left: {eta}"
        except KeyError:
            message = "processing"
        mess_dict = {
            "status": "message:download",
            "level": "info",
            "title": title,
            "message": message,
        }
        RedisArchivist().set_message("message:download", mess_dict)

    def _build_obs(self):
        """collection to build all obs passed to yt-dlp"""
        self._build_obs_basic()
        self._build_obs_user()
        self._build_obs_postprocessors()

    def _build_obs_basic(self):
        """initial obs"""
        self.obs = {
            "default_search": "ytsearch",
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "outtmpl": (
                self.config["application"]["cache_dir"]
                + "/download/"
                + self.config["application"]["file_template"]
            ),
            "progress_hooks": [self._progress_hook],
            "noprogress": True,
            "quiet": True,
            "continuedl": True,
            "retries": 3,
            "writethumbnail": False,
            "noplaylist": True,
            "check_formats": "selected",
        }

    def _build_obs_user(self):
        """build user customized options"""
        if self.config["downloads"]["format"]:
            self.obs["format"] = self.config["downloads"]["format"]
        if self.config["downloads"]["limit_speed"]:
            self.obs["ratelimit"] = (
                self.config["downloads"]["limit_speed"] * 1024
            )

        throttle = self.config["downloads"]["throttledratelimit"]
        if throttle:
            self.obs["throttledratelimit"] = throttle * 1024

    def _build_obs_postprocessors(self):
        """add postprocessor to obs"""
        postprocessors = []

        if self.config["downloads"]["add_metadata"]:
            postprocessors.append(
                {
                    "key": "FFmpegMetadata",
                    "add_chapters": True,
                    "add_metadata": True,
                }
            )

        if self.config["downloads"]["add_thumbnail"]:
            postprocessors.append(
                {
                    "key": "EmbedThumbnail",
                    "already_have_thumbnail": True,
                }
            )
            self.obs["writethumbnail"] = True

        self.obs["postprocessors"] = postprocessors

    def _dl_single_vid(self, youtube_id):
        """download single video"""
        dl_cache = self.config["application"]["cache_dir"] + "/download/"

        # check if already in cache to continue from there
        all_cached = ignore_filelist(os.listdir(dl_cache))
        for file_name in all_cached:
            if youtube_id in file_name:
                self.obs["outtmpl"] = os.path.join(dl_cache, file_name)
        with yt_dlp.YoutubeDL(self.obs) as ydl:
            try:
                ydl.download([youtube_id])
            except yt_dlp.utils.DownloadError:
                print("retry failed download: " + youtube_id)
                sleep(10)
                ydl.download([youtube_id])

        if self.obs["writethumbnail"]:
            # webp files don't get cleaned up automatically
            all_cached = ignore_filelist(os.listdir(dl_cache))
            to_clean = [i for i in all_cached if not i.endswith(".mp4")]
            for file_name in to_clean:
                file_path = os.path.join(dl_cache, file_name)
                os.remove(file_path)

    def move_to_archive(self, vid_dict):
        """move downloaded video from cache to archive"""
        videos = self.config["application"]["videos"]
        host_uid = self.config["application"]["HOST_UID"]
        host_gid = self.config["application"]["HOST_GID"]
        channel_name = clean_string(vid_dict["channel"]["channel_name"])
        if len(channel_name) <= 3:
            # fall back to channel id
            channel_name = vid_dict["channel"]["channel_id"]
        # make archive folder with correct permissions
        new_folder = os.path.join(videos, channel_name)
        if not os.path.exists(new_folder):
            os.makedirs(new_folder)
            if host_uid and host_gid:
                os.chown(new_folder, host_uid, host_gid)
        # find real filename
        cache_dir = self.config["application"]["cache_dir"]
        all_cached = ignore_filelist(os.listdir(cache_dir + "/download/"))
        for file_str in all_cached:
            if vid_dict["youtube_id"] in file_str:
                old_file = file_str
        old_file_path = os.path.join(cache_dir, "download", old_file)
        new_file_path = os.path.join(videos, vid_dict["media_url"])
        # move media file and fix permission
        shutil.move(old_file_path, new_file_path)
        if host_uid and host_gid:
            os.chown(new_file_path, host_uid, host_gid)

    def _delete_from_pending(self, youtube_id):
        """delete downloaded video from pending index if its there"""
        es_url = self.config["application"]["es_url"]
        es_auth = self.config["application"]["es_auth"]
        url = f"{es_url}/ta_download/_doc/{youtube_id}"
        response = requests.delete(url, auth=es_auth)
        if not response.ok and not response.status_code == 404:
            print(response.text)

    def _add_subscribed_channels(self):
        """add all channels subscribed to refresh"""
        all_subscribed = PlaylistSubscription().get_playlists()
        if not all_subscribed:
            return

        channel_ids = [i["playlist_channel_id"] for i in all_subscribed]
        for channel_id in channel_ids:
            self.channels.add(channel_id)

        return

    def validate_playlists(self):
        """look for playlist needing to update"""
        print("sync playlists")
        self._add_subscribed_channels()
        all_indexed = PendingList().get_all_indexed()
        all_youtube_ids = [i["youtube_id"] for i in all_indexed]
        for id_c, channel_id in enumerate(self.channels):
            playlists = YoutubeChannel(channel_id).get_indexed_playlists()
            all_playlist_ids = [i["playlist_id"] for i in playlists]
            for id_p, playlist_id in enumerate(all_playlist_ids):
                playlist = YoutubePlaylist(playlist_id)
                playlist.all_youtube_ids = all_youtube_ids
                playlist.build_json(scrape=True)
                if not playlist.json_data:
                    playlist.deactivate()

                playlist.add_vids_to_playlist()
                playlist.upload_to_es()

                # notify
                title = (
                    "Processing playlists for channels: "
                    + f"{id_c + 1}/{len(self.channels)}"
                )
                message = f"Progress: {id_p + 1}/{len(all_playlist_ids)}"
                mess_dict = {
                    "status": "message:download",
                    "level": "info",
                    "title": title,
                    "message": message,
                }
                if id_p + 1 == len(all_playlist_ids):
                    RedisArchivist().set_message(
                        "message:download", mess_dict, expire=4
                    )
                else:
                    RedisArchivist().set_message("message:download", mess_dict)

    @staticmethod
    def auto_delete_watched(autodelete_days):
        """delete watched videos after x days"""
        now = int(datetime.now().strftime("%s"))
        now_lte = now - autodelete_days * 24 * 60 * 60
        data = {
            "query": {"range": {"player.watched_date": {"lte": now_lte}}},
            "sort": [{"player.watched_date": {"order": "asc"}}],
        }
        all_to_delete = IndexPaginate("ta_video", data).get_results()
        all_youtube_ids = [i["youtube_id"] for i in all_to_delete]
        if not all_youtube_ids:
            return

        for youtube_id in all_youtube_ids:
            print(f"autodelete {youtube_id}")
            YoutubeVideo(youtube_id).delete_media_file()

        print("add deleted to ignore list")
        pending_handler = PendingList()
        pending_handler.add_to_pending(all_youtube_ids, ignore=True)
