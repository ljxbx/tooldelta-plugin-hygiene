"""私人点歌插件。

免费点歌，只对点歌人播放；支持本地曲库的分页与模糊搜索，
以及通过 MIDI 中转服务（midishow）搜索并下载曲目。

网络歌源（2026-10-19 起）：
    主链路为自建中转服务 ``https://midishow.tdgzs.top``，三步走：
    ``/api/search`` 搜索 -> ``/api/fetch`` 取签名直链 -> ``GET <url>`` 下文件。
    历史链路（midishow 直连搜索 + midi.fxdby.net 下载）已废弃但保留在
    文件尾部，见「废弃链路（LEGACY）」分区注释。
"""

import base64
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from tooldelta import Player, Plugin, cfg, plugin_entry, utils

MENU_TIMEOUT = 300

# ---------------------------------------------------------------------------
# MIDI 中转服务常量
#
# 服务端自己去 midishow 拉曲并缓存
# ---------------------------------------------------------------------------
NETWORK_DEFAULT_BASE = "https://midishow.tdgzs.top"

NETWORK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Windows 保留文件名（即使用作主名 + 扩展名，系统也会拒绝）
WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CFG_STD = {
    "本地歌曲点歌每页显示多少歌曲": cfg.PInt,
    "是否启用网络点歌": bool,
    "网络点歌冷却秒数": cfg.NNInt,
    "网络点歌每日上限": cfg.NNInt,
    "网络点歌搜索结果每页显示": cfg.PInt,
    "是否保留原始 MIDI": bool,
    "播放时是否公告": bool,
    "网络请求间隔秒数": cfg.NNNumber,
    "网络请求超时秒数": cfg.PInt,
    "网络请求失败重试次数": cfg.NNInt,
    "网络点歌服务地址": str,
    "网络点歌客户端标识": str,
}

CFG_DEFAULT = {
    "本地歌曲点歌每页显示多少歌曲": 10,
    "是否启用网络点歌": True,
    "网络点歌冷却秒数": 30,
    "网络点歌每日上限": 0,
    "网络点歌搜索结果每页显示": 10,
    "是否保留原始 MIDI": True,
    "播放时是否公告": False,
    "网络请求间隔秒数": 1.0,
    "网络请求超时秒数": 15,
    "网络请求失败重试次数": 1,
    "网络点歌服务地址": NETWORK_DEFAULT_BASE,
    "网络点歌客户端标识": "",
}


@dataclass
class SongSearchResult:
    """midishow 搜索结果条目（仅废弃链路使用）。"""

    title: str
    link: str
    download_count: str = "?"
    rating: str = "?"
    duration: str = "?"


@dataclass
class NetworkSong:
    """中转服务返回的一首曲目（即 ``/api/search`` 的 results 单项）。

    Attributes:
        song_id: 作品 ID，``/api/fetch`` 用它换取下载直链。
        title: 曲名。
        author: 上传者。
        duration: 时长文本，形如 ``04:19``；服务端缺失时回落为 ``?``。
        downloads: 站点下载量。
        size_text: 文件大小文本，形如 ``25.42 KB``。
        tracks: 轨道数。
    """

    song_id: str
    title: str
    author: str = ""
    duration: str = "?"
    downloads: int = 0
    size_text: str = ""
    tracks: int = 0


class PersonalSongRequest(Plugin):
    """私人点歌主插件类。"""

    name = "私人点歌"
    author = "小六神"
    version = (0, 2, 0)
    description = (
        "免费点歌，只对点歌人播放；支持本地曲库分页与模糊搜索，"
        "以及通过 MIDI 中转服务（midishow）搜索并下载曲目。"
    )

    def __init__(self, frame):
        """初始化配置、数据目录与运行时状态。"""
        super().__init__(frame)
        self.load_config()
        self.make_data_path()
        self.music_dir = self.format_data_path("音乐列表")
        os.makedirs(self.music_dir, exist_ok=True)

        # 本地曲库：曲名列表 + 已载入播放器的 mtime 缓存
        self.local_songs: list[str] = []
        self.loaded_songs: dict[str, float] = {}
        # 网络曲库索引：本地曲名 -> {"id": 作品ID, "title": 原始曲名}
        self.network_index: dict[str, dict[str, Any]] = {}
        # 播放状态：玩家名 -> MIDI播放器线程 ID
        self.playing_threads: dict[str, int] = {}
        # 网络点歌限流：冷却时间戳 / (日期, 当日次数)
        self.network_cooldowns: dict[str, float] = {}
        self.network_daily: dict[str, tuple[str, int]] = {}

        # 网络请求全局串行锁：避免多玩家同时打第三方服务触发限流
        self.network_lock = threading.Lock()
        self.network_last_request = 0.0

        self.chatbar = None
        self.midiplayer = None
        self.requests = None
        self.urllib3 = None
        self.etree = None  # 仅废弃链路使用

        self.ListenPreload(self.on_preload)
        self.ListenActive(self.on_active)
        self.ListenPlayerLeave(self.on_player_leave)
        self.ListenFrameExit(self.on_frame_exit)

    # ------------------------------------------------------------------
    # 配置与生命周期
    # ------------------------------------------------------------------
    def load_config(self):
        """读取配置；插件升级后自动补齐新增键，再做一次严格类型校验。"""
        self.cfg, cfg_ver = cfg.get_plugin_config_and_version(
            self.name, {}, CFG_DEFAULT, self.version
        )
        if cfg_ver < self.version:
            added = [key for key in CFG_DEFAULT if key not in self.cfg]
            for key in added:
                self.cfg[key] = CFG_DEFAULT[key]
            if added:
                cfg.upgrade_plugin_config(self.name, self.cfg, self.version)
        self.cfg, _ = cfg.get_plugin_config_and_version(
            self.name, CFG_STD, CFG_DEFAULT, self.version
        )

    @property
    def page_size(self) -> int:
        """本地曲库每页显示的曲目数。"""
        return max(1, int(self.cfg["本地歌曲点歌每页显示多少歌曲"]))

    @property
    def network_base(self) -> str:
        """网络点歌服务地址（去掉末尾斜杠；留空则回落默认地址）。"""
        base = str(self.cfg.get("网络点歌服务地址") or "").strip()
        return (base or NETWORK_DEFAULT_BASE).rstrip("/")

    @property
    def network_page_size(self) -> int:
        """网络搜索结果每页显示条数。"""
        return max(1, int(self.cfg["网络点歌搜索结果每页显示"]))

    def on_preload(self):
        """前置阶段：取前置插件 API、装依赖、载入本地曲库与网络索引。"""
        self.prepare_plugin_apis()
        self.prepare_network_dependencies()
        self.network_index = self.load_network_index()
        self.refresh_library(force=True)

    def on_active(self):
        """激活阶段：注册聊天栏触发词与控制台命令。"""
        self.chatbar.add_new_trigger(
            [".点歌", ".song"],
            [],
            "打开私人点歌菜单",
            self.on_song_command,
        )
        self.chatbar.add_new_trigger(
            [".停止点歌", ".stopsong"],
            [],
            "停止你当前播放的歌曲",
            self.stop_current_song,
        )
        self.chatbar.add_new_trigger(
            [".重载曲库", ".reloadsongs"],
            [],
            "重新扫描私人点歌的本地曲库",
            self.reload_library_command,
            op_only=True,
        )
        self.frame.add_console_cmd_trigger(
            ["重载私人点歌曲库"],
            None,
            "强制重新扫描私人点歌的本地曲库",
            self.on_console_reload,
        )

    def on_player_leave(self, player: Player):
        """玩家离线：停掉他的播放线程并清理冷却记录。

        注意：ToolDelta 的玩家进/离事件是靠解析聊天栏里的
        ``%multiplayer.player.left`` 文本触发的，玩家掉线重连、短暂抖动、
        甚至换维度时都可能收到这条文本。所以这里用 ``online`` 做二次确认，
        避免把还在听歌的玩家误判成已离开而中断播放。
        """
        name = getattr(player, "name", None)
        if not name:
            return
        if getattr(player, "online", None):
            return
        self.stop_song_thread(name)
        self.network_cooldowns.pop(name, None)

    def on_frame_exit(self, _):
        """框架退出：停掉所有仍在播放的线程。"""
        for player_name in list(self.playing_threads):
            self.stop_song_thread(player_name)

    def prepare_plugin_apis(self):
        """获取前置插件 API 并声明第三方 Python 依赖。"""
        pip = self.GetPluginAPI("pip")
        self.chatbar = self.GetPluginAPI("聊天栏菜单")
        self.midiplayer = self.GetPluginAPI("MIDI播放器")
        pip.require(
            {
                "lxml": "lxml",
                "requests": "requests",
                "urllib3": "urllib3",
            }
        )

    def prepare_network_dependencies(self):
        """导入网络依赖并关闭证书告警（pip.require 已保证装好）。"""
        from lxml import etree  # type: ignore
        import requests  # type: ignore
        import urllib3  # type: ignore

        self.etree = etree
        self.requests = requests
        self.urllib3 = urllib3
        self.urllib3.disable_warnings(self.urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------------
    # 玩家提示与菜单骨架
    # ------------------------------------------------------------------
    def show_info(self, player: Player, text: str):
        """向玩家输出普通提示。"""
        player.show(f"§f❀ {text}")

    def show_success(self, player: Player, text: str):
        """向玩家输出成功提示。"""
        player.show(f"§a❀ {text}")

    def show_warn(self, player: Player, text: str):
        """向玩家输出警告提示。"""
        player.show(f"§6❀ {text}")

    def show_error(self, player: Player, text: str):
        """向玩家输出错误提示。"""
        player.show(f"§c❀ {text}")

    @staticmethod
    def menu_header(title: str, with_rule: bool = True) -> str:
        """菜单顶部装饰条。

        Args:
            title: 菜单标题。
            with_rule: 是否附带下方的分隔装饰线。像「网络搜索点歌」这种
                标题下面紧跟 ``menu_footer`` 的纯提示菜单要传 False，
                否则两条装饰线会叠在一起，看着像重复了一条。
        """
        header = f"§l§e>§a>§6> §7「§b{title}§7」\n"
        if not with_rule:
            return header
        return (
            header
            + "§d✧✦§f〓〓§b〓〓〓§9〓〓〓〓§1〓〓〓〓〓〓§9〓〓〓〓§b〓〓〓§f〓〓§d✦✧\n"
        )

    @staticmethod
    def menu_footer(page_label: str, body: str) -> str:
        """菜单底部装饰条与操作提示。"""
        return (
            f"§l§d✧✦§f〓〓§b〓〓〓§9〓〓〓〓 §r§7[ §b{page_label} §7] "
            "§l§9〓〓〓〓§b〓〓〓§f〓〓§d✦✧\n"
            f"§r{body}"
        )

    def prompt_player(self, player: Player, prompt: str) -> str | None:
        """向玩家索取一次输入，超时返回 None 并提示。"""
        response = player.input(prompt, timeout=MENU_TIMEOUT)
        if response is None:
            self.show_error(player, "操作超时，已退出")
            return None
        return response.strip()

    def get_page_items(
        self, items: list[Any], current_page: int, page_size: int
    ) -> tuple[list[Any], int, int]:
        """按页大小切分列表，并把页码夹到合法范围内。"""
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        current_page = max(0, min(current_page, total_pages - 1))
        start = current_page * page_size
        end = min(start + page_size, len(items))
        return items[start:end], current_page, total_pages

    def next_page(self, player: Player, current_page: int, total_pages: int) -> int:
        """翻到下一页；已是最后一页则提示并原地不动。"""
        if current_page + 1 < total_pages:
            return current_page + 1
        self.show_warn(player, "已经是最后一页")
        return current_page

    def previous_page(self, player: Player, current_page: int) -> int:
        """翻到上一页；已是第一页则提示并原地不动。"""
        if current_page > 0:
            return current_page - 1
        self.show_warn(player, "已经是第一页")
        return current_page

    def select_indexed_item(self, player: Player, response: str, items: list[Any]):
        """把玩家输入的序号翻译成列表项；无效输入返回 None 并提示。"""
        choice = utils.try_int(response)
        if choice is None:
            self.show_error(player, "输入无效，请输入序号、q、+、- 或 s")
            return None
        if choice not in range(1, len(items) + 1):
            self.show_error(player, "序号超出当前页范围")
            return None
        return items[choice - 1]

    # ------------------------------------------------------------------
    # 本地曲库：扫描 / 转换 / 增量载入
    # ------------------------------------------------------------------
    def refresh_library(self, force: bool = False) -> int:
        """刷新本地曲库（按 mtime 增量处理，不重复做重活）。

        Args:
            force: 为 True 时忽略 mtime 缓存，全部重新载入。

        Returns:
            本次新载入的曲目数。
        """
        self.sync_seq_files()
        return self.load_local_song_library(force=force)

    def sync_seq_files(self) -> int:
        """把比同名 .midseq 更新的 .mid 转换成 .midseq。

        Returns:
            实际转换的文件数。
        """
        try:
            filenames = os.listdir(self.music_dir)
        except OSError as err:
            self.print_err(f"读取曲库目录失败: {err}")
            return 0
        converted = 0
        for filename in filenames:
            root, ext = os.path.splitext(filename)
            if ext.lower() != ".mid":
                continue
            midi_path = os.path.join(self.music_dir, filename)
            seq_path = os.path.join(self.music_dir, root + ".midseq")
            try:
                if os.path.isfile(seq_path):
                    stale = os.path.getmtime(midi_path) > os.path.getmtime(seq_path)
                else:
                    stale = True
            except OSError:
                stale = True
            if not stale:
                continue
            try:
                self.midiplayer.translate_midi_to_seq_file(midi_path, seq_path)
            except (Exception, SystemExit) as err:
                # MIDI播放器内部失败时会 raise SystemExit，它不属于 Exception
                self.print_err(f"转换 MIDI 失败: {filename} -> {err}")
                continue
            converted += 1
        return converted

    def load_local_song_library(self, force: bool = False) -> int:
        """把曲库目录下的 .midseq 增量载入 MIDI 播放器。

        Args:
            force: 为 True 时忽略 mtime 缓存，全部重新载入。

        Returns:
            本次新载入的曲目数。
        """
        try:
            filenames = sorted(os.listdir(self.music_dir), key=str.lower)
        except OSError as err:
            self.print_err(f"读取曲库目录失败: {err}")
            return 0
        found: list[str] = []
        loaded = 0
        for filename in filenames:
            root, ext = os.path.splitext(filename)
            if ext.lower() != ".midseq":
                continue
            seq_path = os.path.join(self.music_dir, filename)
            try:
                mtime = os.path.getmtime(seq_path)
            except OSError:
                continue
            found.append(root)
            if not force and self.loaded_songs.get(root) == mtime:
                continue
            try:
                self.midiplayer.load_sound_seq_file(seq_path, root)
            except (Exception, SystemExit) as err:
                # 损坏的序列文件同样会 raise SystemExit，必须显式吞掉
                self.print_err(f"载入音乐序列失败: {filename} -> {err}")
                continue
            self.loaded_songs[root] = mtime
            loaded += 1
        for gone in set(self.loaded_songs) - set(found):
            self.loaded_songs.pop(gone, None)
        self.local_songs = found
        return loaded

    def on_console_reload(self, _):
        """控制台命令：强制重载曲库与网络索引。"""
        self.network_index = self.load_network_index()
        self.refresh_library(force=True)

    # ------------------------------------------------------------------
    # 网络曲库索引
    # ------------------------------------------------------------------
    def load_network_index(self) -> dict[str, dict[str, Any]]:
        """读取网络曲库索引（记录已下载曲目对应的中转服务作品 ID）。"""
        path = self.format_data_path("网络曲库索引.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as err:
            self.print_err(f"读取网络曲库索引失败: {err}")
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def save_network_index(self):
        """原子写入网络曲库索引。"""
        path = self.format_data_path("网络曲库索引.json")
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(self.network_index, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as err:
            self.print_err(f"写入网络曲库索引失败: {err}")
            self.remove_file_quietly(tmp_path)

    def resolve_local_name(self, song: NetworkSong) -> str:
        """决定本地文件名，避免同名不同曲互相覆盖。

        Args:
            song: 已拿到作品 ID 的曲目。

        Returns:
            可安全用作文件名的本地曲名。
        """
        base = self.make_safe_song_name(song.title)
        # 变量名避开模块级的 entry（插件入口），否则会遮蔽外层同名
        index_entry = self.network_index.get(base)
        if index_entry is not None and str(index_entry.get("id")) == song.song_id:
            return base
        if index_entry is None and base not in self.local_songs:
            return base
        short = self.make_safe_song_name(song.title[:60])
        return self.make_safe_song_name(f"{short}_{song.song_id[-6:]}")

    # ------------------------------------------------------------------
    # 网络点歌客户端（搜索 / 取直链 / 下载）
    # ------------------------------------------------------------------
    def network_headers(self, with_json: bool = False) -> dict[str, str]:
        """构造请求头。

        服务端前面挂着 WAF，``User-Agent`` 必须是浏览器 UA，否则直接 403。
        配置了客户端标识时一并带上，让服务端按「客户端」而不是「IP」计配额。
        """
        headers = {
            "User-Agent": NETWORK_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if with_json:
            headers["Content-Type"] = "application/json"
        client_id = str(self.cfg.get("网络点歌客户端标识") or "").strip()
        if client_id:
            headers["X-Client-Id"] = client_id
        return headers

    def network_api(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """调用一次中转服务 JSON 接口：全局串行 + 限速 + 重试。

        Args:
            path: 以 ``/`` 开头的接口路径，或者完整 URL。
            payload: 非 None 时改用 POST 发送该 JSON。

        Returns:
            ``(响应字典, 错误文本)``；成功时错误文本为空串。

        Note:
            服务端用 429 表示「该作品今日下载次数已用完」，但响应体仍是
            合法 JSON（``error`` / ``used`` / ``limit``）。所以这里先解析
            JSON 再看状态码，好把这类业务错误原样翻译给玩家。
        """
        attempts = max(1, int(self.cfg["网络请求失败重试次数"]) + 1)
        timeout = max(1, int(self.cfg["网络请求超时秒数"]))
        interval = max(0.0, float(self.cfg["网络请求间隔秒数"]))
        url = path if path.startswith("http") else self.network_base + path
        is_post = payload is not None

        last_err = "未知错误"
        with self.network_lock:
            for attempt in range(attempts):
                wait = interval - (time.time() - self.network_last_request)
                if wait > 0:
                    time.sleep(wait)
                try:
                    if is_post:
                        response = self.requests.post(
                            url,
                            json=payload,
                            headers=self.network_headers(with_json=True),
                            timeout=timeout,
                            verify=False,
                            allow_redirects=True,
                        )
                    else:
                        response = self.requests.get(
                            url,
                            headers=self.network_headers(),
                            timeout=timeout,
                            verify=False,
                            allow_redirects=True,
                        )
                except Exception as err:
                    self.network_last_request = time.time()
                    last_err = f"{type(err).__name__}: {err}"
                else:
                    self.network_last_request = time.time()
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        return data, ""
                    last_err = f"HTTP {response.status_code}"
                    # 4xx 是调用方问题（参数 / 配额 / UA），重试没有意义
                    if 400 <= response.status_code < 500:
                        return None, last_err
                if attempt + 1 < attempts:
                    time.sleep(min(max(interval, 0.5), 3.0))
        self.print_err(f"网络请求失败 {url} -> {last_err}")
        return None, last_err

    def network_get_bytes(self, url: str) -> bytes | None:
        """按签名直链取文件字节。

        ``?t=`` 签名只有 15 分钟且绑定客户端，拿到就该立刻用掉。

        Args:
            url: ``/api/fetch`` 返回的完整直链。

        Returns:
            文件内容；失败返回 None。
        """
        timeout = max(1, int(self.cfg["网络请求超时秒数"]))
        # ?t= 签名绑定客户端：这里必须与 /api/fetch 用同一套请求头，
        # 否则配置了「网络点歌客户端标识」时直链会直接 403。
        try:
            response = self.requests.get(
                url,
                headers=self.network_headers(),
                timeout=timeout,
                verify=False,
                allow_redirects=True,
            )
        except Exception as err:
            self.print_err(f"下载失败 {url} -> {type(err).__name__}: {err}")
            return None
        if response.status_code != 200:
            self.print_err(f"下载失败 {url} -> HTTP {response.status_code}")
            return None
        return response.content

    def network_search(self, keyword: str) -> list[NetworkSong]:
        """在 MIDI 中转服务上搜索曲目。

        Args:
            keyword: 搜索关键词。

        Returns:
            搜索结果列表；失败返回空列表。
        """
        path = "/api/search?q=" + urllib.parse.quote(keyword) + "&page=1"
        data, err = self.network_api(path)
        if data is None:
            self.print_err(f"搜索失败 {keyword} -> {err}")
            return []
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            self.print_err(f"搜索返回结构异常: {str(data)[:200]}")
            return []
        songs: list[NetworkSong] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("id") or "").strip()
            if not song_id:
                continue
            songs.append(
                NetworkSong(
                    song_id=song_id,
                    title=str(item.get("title") or "").strip() or "未命名歌曲",
                    author=str(item.get("author") or "").strip(),
                    duration=str(item.get("duration") or "?") or "?",
                    downloads=int(item.get("downloads") or 0),
                    size_text=str(item.get("size") or ""),
                    tracks=int(item.get("tracks") or 0),
                )
            )
        return songs

    def network_fetch_url(self, song: NetworkSong) -> tuple[str, str]:
        """用作品 ID 换一次下载直链。

        Args:
            song: 玩家选中的曲目。

        Returns:
            ``(下载直链, 错误文本)``；成功时错误文本为空串。
        """
        data, err = self.network_api("/api/fetch", {"id": song.song_id})
        if data is None:
            return "", err
        url = str(data.get("url") or "").strip()
        if not url:
            return "", str(data.get("error") or "服务端未返回下载地址")
        if url.startswith("/"):
            url = self.network_base + url
        return url, ""

    def network_download(self, url: str) -> bytes | None:
        """下载曲目文件并校验 MIDI 头。

        Args:
            url: 服务端签发的签名直链。

        Returns:
            MIDI 二进制内容；失败返回 None。
        """
        data = self.network_get_bytes(url)
        if data is None:
            return None
        if data[:4] != b"MThd":
            self.print_err(f"下载内容不是 MIDI（前 8 字节 {data[:8]!r}）")
            return None
        return data

    def install_network_song(self, song: NetworkSong, data: bytes) -> str | None:
        """把下载到的 MIDI 落盘、转换、载入曲库并登记索引。

        Args:
            song: 已下载的曲目。
            data: MIDI 二进制内容。

        Returns:
            本地曲名；失败返回 None。
        """
        local_name = self.resolve_local_name(song)
        midi_path = os.path.join(self.music_dir, local_name + ".mid")
        seq_path = os.path.join(self.music_dir, local_name + ".midseq")
        try:
            with open(midi_path, "wb") as file:
                file.write(data)
            self.midiplayer.translate_midi_to_seq_file(midi_path, seq_path)
        except (Exception, SystemExit) as err:
            self.print_err(f"写入或转换下载的 MIDI 失败: {local_name} -> {err}")
            self.remove_file_quietly(midi_path)
            self.remove_file_quietly(seq_path)
            return None
        if not self.cfg["是否保留原始 MIDI"]:
            self.remove_file_quietly(midi_path)
        self.load_local_song_library()
        self.network_index[local_name] = {
            "id": song.song_id,
            "title": song.title,
        }
        self.save_network_index()
        return local_name

    def check_network_cooldown(self, player: Player) -> bool:
        """检查网络点歌冷却，通过则记录本次时间。

        Args:
            player: 发起点歌的玩家。

        Returns:
            允许继续则为 True。
        """
        cooldown = max(0, int(self.cfg["网络点歌冷却秒数"]))
        now = time.time()
        last = self.network_cooldowns.get(player.name)
        if cooldown > 0 and last is not None:
            remain = cooldown - (now - last)
            if remain > 0:
                self.show_warn(player, f"网络点歌冷却中，请 {int(remain) + 1} 秒后再试")
                return False
        self.network_cooldowns[player.name] = now
        return True

    def check_network_daily_limit(self, player: Player) -> bool:
        """检查并消耗当日网络点歌额度（在真正下载成功时调用）。

        Args:
            player: 发起点歌的玩家。

        Returns:
            未超限则为 True。
        """
        limit = max(0, int(self.cfg["网络点歌每日上限"]))
        if limit <= 0:
            return True
        today = time.strftime("%Y-%m-%d")
        record = self.network_daily.get(player.name)
        if record is None or record[0] != today:
            record = (today, 0)
        if record[1] >= limit:
            self.network_daily[player.name] = record
            self.show_warn(player, f"今天的网络点歌次数已用完（上限 {limit} 首）")
            return False
        self.network_daily[player.name] = (record[0], record[1] + 1)
        return True

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------
    def on_song_command(self, player: Player, _args: tuple):
        """聊天栏入口 ``.点歌``：打开主菜单。

        这里刻意声明 ``argument_hint=[]``（不接受参数）。实测部分版本的
        聊天栏菜单前置会把 ``...``（任意参数）理解为「必须补参数」，
        于是玩家只发 ``.点歌`` 时被前置插件反问 ``请输入：``。
        带歌名直达改由菜单内的输入兜底，见 ``open_song_menu`` 末尾分支。
        """
        self.open_song_menu(player, ())

    def search_and_play(self, player: Player, keyword: str):
        """先匹配本地曲库；本地没有则询问是否转向网络搜索。"""
        self.refresh_library()
        matches = [
            name for name in self.local_songs if self.is_fuzzy_match(name, keyword)
        ]
        if len(matches) == 1:
            self.play_song_for_player(player, matches[0])
            return
        if matches:
            chosen = self.choose_local_song(
                player, matches, f"本地歌曲点歌 - 匹配: {keyword}"
            )
            if chosen is not None:
                self.play_song_for_player(player, chosen)
            return
        if not self.cfg["是否启用网络点歌"]:
            self.show_error(player, f"本地没有包含 §6{keyword}§c 的歌曲。")
            return
        self.show_warn(player, f"本地没有包含 §6{keyword}§c 的歌曲。")
        answer = self.prompt_player(player, "§a是否从网络搜索？输入 §ey§a 继续：")
        if answer is None:
            return
        if answer.strip().lower() not in ("y", "yes", "是", "好"):
            self.show_info(player, "已取消网络搜索")
            return
        self.handle_network_search(player, keyword)

    def stop_current_song(self, player: Player, _):
        """聊天栏入口 ``.停止点歌``。"""
        if self.stop_song_thread(player.name):
            self.show_success(player, "已停止你当前播放的歌曲")
        else:
            self.show_warn(player, "你当前没有正在播放的歌曲")

    def reload_library_command(self, player: Player, _):
        """聊天栏入口 ``.重载曲库``（仅 OP 在菜单中可见）。"""
        if not player.is_op():
            self.show_error(player, "只有 OP 可以重载曲库")
            return
        loaded = self.refresh_library(force=True)
        self.show_success(
            player,
            f"曲库已重载：共 {len(self.local_songs)} 首，本次载入 {loaded} 首",
        )

    # ------------------------------------------------------------------
    # 菜单流程
    # ------------------------------------------------------------------
    def open_song_menu(self, player: Player, _):
        """``.点歌`` 的主菜单。"""
        self.refresh_library()
        if not self.cfg["是否启用网络点歌"]:
            self.handle_local_song_menu(player)
            return
        # 逐行发送，避免整块多行文本在某些接入点下渲染成一行而被玩家忽略
        player.show(self.menu_header("私人点歌"))
        player.show("§l§b[ §e1 §b] §f- §7本地点歌")
        player.show("§l§b[ §e2 §b] §f- §7网络搜索点歌")
        player.show(
            self.menu_footer(
                "MENU",
                "§a❀ §b输入 §e1 §b进入本地点歌\n"
                "§a❀ §b输入 §e2 §b进入网络搜索\n"
                "§a❀ §b输入 §6q §r§b退出菜单",
            )
        )
        # 操作提示已经写在页脚，输入提示留空，避免屏幕上多一行重复文本
        choice = self.prompt_player(player, "")
        if choice is None:
            return
        lowered = choice.strip().lower()
        if not lowered:
            # 没有输入提示行，玩家可能直接回车退出；空输入不当作歌名处理
            return
        if lowered == "q":
            self.show_error(player, "已退出点歌菜单")
            return
        if lowered == "1":
            self.handle_local_song_menu(player)
            return
        if lowered == "2":
            self.handle_network_song_menu(player)
            return
        # 既不是选项也不是退出指令，就按歌名处理，省得玩家再输一遍
        self.show_info(player, f"未选择模式，按歌名查找：§f{choice}§f")
        self.search_and_play(player, choice)

    def handle_local_song_menu(self, player: Player):
        """进入本地曲库选择流程。"""
        if not self.local_songs:
            self.show_error(
                player,
                f"本地曲库为空，请把 .mid 或 .midseq 放入 "
                f"§6插件数据文件/{self.name}/音乐列表§c。",
            )
            return
        selected_song = self.choose_local_song(player)
        if selected_song is not None:
            self.play_song_for_player(player, selected_song)

    def choose_local_song(
        self,
        player: Player,
        source_songs: list[str] | None = None,
        title: str | None = None,
    ) -> str | None:
        """分页展示本地歌曲并让玩家选择，支持模糊搜索。

        Args:
            player: 选择的玩家。
            source_songs: 候选曲名；默认使用整个本地曲库。
            title: 自定义菜单标题；留空时按是否有搜索关键词自动生成。

        Returns:
            选中的曲名；退出或超时返回 None。
        """
        source = list(source_songs if source_songs is not None else self.local_songs)
        songs = list(source)
        keyword = ""
        current_page = 0

        while True:
            if not songs:
                if keyword:
                    self.show_error(player, f"没有找到包含 §6{keyword}§c 的歌曲。")
                    songs = list(source)
                    keyword = ""
                    current_page = 0
                else:
                    self.show_error(player, "本地曲库为空。")
                    return None

            page_songs, current_page, total_pages = self.get_page_items(
                songs, current_page, self.page_size
            )
            header = title or (
                "本地歌曲点歌" if not keyword else f"本地歌曲点歌 - 搜索: {keyword}"
            )
            player.show(self.menu_header(header))
            for index, song_name in enumerate(page_songs, start=1):
                player.show(f"§l§b[ §e{index} §b] §f- §7{song_name}")
            player.show(
                self.menu_footer(
                    f"{current_page + 1}/{total_pages}",
                    "§f输入当前页歌名前序号点歌\n"
                    "输入 §l§b+ §7/ §c- §r§f翻页\n"
                    "输入 §l§6q §r§f退出菜单\n"
                    "输入 §l§es§r§f 搜索",
                )
            )

            response = self.prompt_player(player, "§a请输入操作：")
            if response is None:
                return None
            lowered = response.lower()
            if lowered == "q":
                self.show_error(player, "已退出点歌菜单")
                return None
            if response == "+":
                current_page = self.next_page(player, current_page, total_pages)
                continue
            if response == "-":
                current_page = self.previous_page(player, current_page)
                continue
            if lowered == "s":
                songs, keyword, current_page = self.search_local_songs(player, source)
                continue

            selected_song = self.select_indexed_item(player, response, page_songs)
            if selected_song is not None:
                return selected_song

    def search_local_songs(
        self, player: Player, source_songs: list[str]
    ) -> tuple[list[str], str, int]:
        """向玩家索取关键词并按模糊规则过滤本地曲库。

        Args:
            player: 搜索的玩家。
            source_songs: 完整候选列表。

        Returns:
            (过滤后的列表, 关键词, 页码)。
        """
        keyword_input = self.prompt_player(
            player, "§a请输入搜索关键词，留空返回完整列表："
        )
        if keyword_input is None:
            self.show_error(player, "搜索超时，保持当前列表")
            return list(source_songs), "", 0
        if not keyword_input:
            return list(source_songs), "", 0
        filtered = [
            name for name in source_songs if self.is_fuzzy_match(name, keyword_input)
        ]
        if not filtered:
            self.show_error(player, f"没有找到包含 §6{keyword_input}§c 的歌曲")
            return list(source_songs), "", 0
        return filtered, keyword_input, 0

    def is_fuzzy_match(self, song_name: str, keyword: str) -> bool:
        """判断歌曲名是否模糊命中关键词（顺序无关的多词匹配）。"""
        normalized_song = self.normalize_text(song_name)
        normalized_keyword = self.normalize_text(keyword)
        if not normalized_keyword:
            return True
        if normalized_keyword in normalized_song:
            return True
        return all(part in normalized_song for part in normalized_keyword.split())

    @staticmethod
    def normalize_text(text: str) -> str:
        """把文本统一成小写、单空格分隔的形式用于比较。"""
        return "".join(ch.lower() if not ch.isspace() else " " for ch in text).strip()

    # ------------------------------------------------------------------
    # 网络点歌流程
    # ------------------------------------------------------------------
    def handle_network_song_menu(self, player: Player):
        """网络点歌入口：询问歌名后走中转服务搜索。"""
        # 这个菜单没有条目列表，标题下直接是页脚装饰线，
        # 所以不画顶部那条装饰线，否则两条线会紧挨着重复。
        player.show(
            self.menu_header("网络搜索点歌", with_rule=False)
            + self.menu_footer(
                "网络点歌",
                "§a❀ §b输入歌曲名开始搜索\n"
                "§a❀ §b输入 §l§6q §r§b退出菜单",
            )
        )
        keyword = self.prompt_player(player, "§a请输入要搜索的歌曲名称：")
        if keyword is None:
            return
        keyword = keyword.strip()
        if not keyword or keyword.lower() == "q":
            self.show_error(player, "已退出网络点歌")
            return
        self.handle_network_search(player, keyword)

    def handle_network_search(self, player: Player, keyword: str):
        """搜索中转服务，展示结果并走下载播放流程。"""
        if not self.check_network_cooldown(player):
            return
        self.show_info(player, f"正在搜索 §f{keyword}§f …")
        results = self.network_search(keyword)
        if not results:
            self.show_error(player, f"没有搜到 §6{keyword}§c。")
            return
        selected = self.display_network_results(results, player, keyword)
        if selected is None:
            return
        self.download_and_play_network(player, selected)

    def display_network_results(
        self, results: list[NetworkSong], player: Player, keyword: str
    ) -> NetworkSong | None:
        """分页展示网络搜索结果并让玩家选一首。

        每个条目的时长与下载量固定用 §f 白色展示，标题用 §7 灰，
        这样一眼就能把「歌名」和「时长 / 下载量」分开。

        Args:
            results: 搜索结果。
            player: 选择的玩家。
            keyword: 关键词，仅用于标题展示。

        Returns:
            选中的曲目；退出或超时返回 None。
        """
        current_page = 0
        while True:
            page_items, current_page, total_pages = self.get_page_items(
                results, current_page, self.network_page_size
            )
            player.show(self.menu_header(f"网络搜索 - {keyword}"))
            for index, item in enumerate(page_items, start=1):
                player.show(
                    f"§l§b[ §e{index} §b] §f- §7{item.title}"
                    f" §8| §f时长 {item.duration}"
                    f" §8| §f下载 {item.downloads}"
                )
            player.show(
                self.menu_footer(
                    f"{current_page + 1}/{total_pages}",
                    "§f输入当前页序号选择歌曲\n"
                    "输入 §l§b+ §7/ §c- §r§f翻页\n"
                    "输入 §l§6q §r§f退出菜单",
                )
            )
            response = self.prompt_player(player, "§a请输入操作：")
            if response is None:
                return None
            lowered = response.lower()
            if lowered == "q":
                self.show_error(player, "已退出网络点歌")
                return None
            if response == "+":
                current_page = self.next_page(player, current_page, total_pages)
                continue
            if response == "-":
                current_page = self.previous_page(player, current_page)
                continue
            selected = self.select_indexed_item(player, response, page_items)
            if isinstance(selected, NetworkSong):
                return selected

    def download_and_play_network(self, player: Player, song: NetworkSong):
        """取直链 -> 下载 -> 入库 -> 为玩家播放。"""
        if not self.check_network_daily_limit(player):
            return
        self.show_info(player, f"正在获取 §f{song.title}§f 的下载地址 …")
        url, err = self.network_fetch_url(song)
        if not url:
            self.show_error(player, err or "获取下载地址失败，请稍后再试")
            return
        self.show_info(player, f"正在下载 §f{song.title}§f …")
        data = self.network_download(url)
        if data is None:
            self.show_error(player, "下载失败，请稍后再试")
            return
        local_name = self.install_network_song(song, data)
        if local_name is None:
            self.show_error(player, "曲子已下载但无法载入，可能是文件格式问题")
            return
        self.show_success(player, f"已存入曲库：§f「 §b{local_name} §f」")
        self.play_song_for_player(player, local_name)
        if self.cfg["播放时是否公告"]:
            self.game_ctrl.say_to("@a", f"§b{song.title} §7正在播放中")

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def play_song_for_player(self, player: Player, song_name: str):
        """停掉该玩家当前播放，再为他播放指定曲目。"""
        self.stop_song_thread(player.name)
        target = utils.to_player_selector(player.name)
        thread_id = self.midiplayer.playsound_at_target(song_name, target)
        if not thread_id:
            self.show_error(player, f"播放失败：无法播放 §6{song_name}§c。")
            return
        self.playing_threads[player.name] = thread_id
        self.show_success(player, f"开始为你播放：§f「 §b{song_name} §f」")

    def is_song_playing(self, player_name: str) -> bool:
        """判断该玩家此刻是否真的在播放。

        MIDI播放器的播放线程结束时会在内部删除自己的记录，
        这里同步清理过期映射，避免 ``.停止点歌`` 出现假阳性。
        """
        thread_id = self.playing_threads.get(player_name)
        if thread_id is None:
            return False
        if self.midiplayer.playsound_threads.get(thread_id) is None:
            self.playing_threads.pop(player_name, None)
            return False
        return True

    def stop_song_thread(self, player_name: str) -> bool:
        """停止该玩家正在播放的歌曲。

        Args:
            player_name: 玩家名。

        Returns:
            是否真的停掉了一首正在播放的曲子。
        """
        thread_id = self.playing_threads.pop(player_name, None)
        if thread_id is None:
            return False
        if self.midiplayer.playsound_threads.get(thread_id) is None:
            return False
        self.midiplayer.stop_playing(thread_id)
        return True

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def remove_file_quietly(path: str):
        """删除文件；不存在或删除失败时静默忽略。"""
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    @staticmethod
    def make_safe_song_name(title: str) -> str:
        """把任意标题清洗成可安全落盘的文件名。

        Args:
            title: 原始标题。

        Returns:
            清洗后的文件名（不含扩展名）。
        """
        cleaned = (title or "").strip() or "未命名歌曲"
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", cleaned)
        cleaned = cleaned[:80].rstrip(". ")
        if not cleaned:
            # 形如 ".." / "..." 的标题，rstrip 之后会变成空串
            return "未命名歌曲"
        if cleaned.upper() in WINDOWS_RESERVED_NAMES:
            return f"_{cleaned}"
        return cleaned

    # ==================================================================
    # 废弃链路（LEGACY）：midishow 搜索 + midi.fxdby.net 下载
    #
    # 已废弃，不参与任何调用路径，仅作留档与回滚参考。
    # 废弃原因（2026-10-17 实测）：
    #   1. midi.fxdby.net/api/download_midi 返回 404，域名根是 MCSManager
    #      Daemon 默认页 —— 该第三方中转服务已下线；
    #   2. midishow 无公开 API，GET /midi/download?id= 要求登录且每首消耗
    #      3 积分（注册验证邮箱仅送 5 分）；
    #   3. midishow 全站 Cloudflare，连续约 5 次请求即 403 且 100 秒不恢复；
    #   4. 试听直链带短期 token，过期即 403，无法稳定复用。
    # 若上述服务将来恢复，可参考本分区低成本回滚（P0 修复已一并同步）。
    # ==================================================================
    def legacy_search_midishow(self, music_name: str, player: Player):
        """[废弃] 在 midishow 搜索曲目，返回 xpath 节点列表。"""
        search_url = "https://www.midishow.com/search/result"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.midishow.com/",
        }
        self.show_info(player, "正在搜索网络歌曲，请稍候…")
        try:
            response = self.requests.get(
                search_url,
                headers=headers,
                params={"q": music_name},
                verify=False,
                timeout=10,
            )
            response.raise_for_status()
        except Exception as err:
            self.show_error(player, f"搜索失败：{err}")
            return None
        tree = self.etree.HTML(response.text)
        return tree.xpath(
            '//div[@id="search-result"]'
            '/div/a[@class="d-block border-bottom pb-5 mb-5 position-relative"]'
        )

    def legacy_parse_search_result(self, node) -> SongSearchResult:
        """[废弃] 把 midishow 搜索结果节点解析成 SongSearchResult。"""
        title = (
            node.xpath(".//h3")[0].xpath("string()").strip()
            if node.xpath(".//h3")
            else "未知标题"
        )
        node_muted = node.xpath('.//div[@class="small text-muted"]')
        info_text = node_muted[0].xpath("string()").strip() if node_muted else ""
        node_file = node.xpath('.//div[@class="row small text-muted pl-0 pl-md-9"]')
        file_info = node_file[0].xpath("string()").strip() if node_file else ""
        download_match = re.search(r"(\d+)次下载", info_text)
        rating_match = re.search(r"(\d+\.\d+)\(\d+人打分\)", file_info)
        duration_match = re.search(r"(\d+:\d+)", file_info)
        return SongSearchResult(
            title=title,
            link=node.get("href") or "",
            download_count=download_match.group(1) if download_match else "?",
            rating=rating_match.group(1) if rating_match else "?",
            duration=duration_match.group(1) if duration_match else "?",
        )

    def legacy_display_search_results(
        self, music_items: list, player: Player, keyword: str
    ):
        """[废弃] 分页展示 midishow 搜索结果并让玩家选择。"""
        if not music_items:
            self.show_error(player, "没有找到相关歌曲")
            return None
        current_page = 0
        while True:
            page_items, current_page, total_pages = self.get_page_items(
                music_items, current_page, self.page_size
            )
            player.show(self.menu_header(f"网络搜索点歌 - 搜索: {keyword}"))
            for index, node in enumerate(page_items, start=1):
                item = self.legacy_parse_search_result(node)
                player.show(
                    f"§l§b[ §e{index} §b] §f- §7{item.title} "
                    f"§8(下载:{item.download_count} 评分:{item.rating} "
                    f"时长:{item.duration})"
                )
            player.show(
                self.menu_footer(
                    f"{current_page + 1}/{total_pages}",
                    "§f输入当前页序号选择歌曲\n"
                    "输入 §l§b+ §7/ §c- §r§f翻页\n"
                    "输入 §l§6q §r§f退出菜单",
                )
            )
            response = self.prompt_player(player, "§a请输入操作：")
            if response is None:
                return None
            lowered = response.lower()
            if lowered == "q":
                self.show_error(player, "已退出网络点歌")
                return None
            if response == "+":
                current_page = self.next_page(player, current_page, total_pages)
                continue
            if response == "-":
                current_page = self.previous_page(player, current_page)
                continue
            selected = self.select_indexed_item(player, response, page_items)
            if selected is not None:
                return selected

    def legacy_download_from_fxdby(self, selected_link: str):
        """[废弃] 调 midi.fxdby.net 中转接口下载 MIDI（该服务已 404）。"""
        try:
            api_response = self.requests.post(
                "https://midi.fxdby.net/api/download_midi",
                json={"url": selected_link},
                verify=False,
                timeout=15,
            )
            api_response.raise_for_status()
            return api_response.json()
        except Exception as err:
            self.print_err(f"网络 MIDI 下载失败: {err}")
            return None

    def legacy_process_downloaded_midi(
        self, api_data: dict, fallback_title: str, player: Player
    ) -> str | None:
        """[废弃] 解析 fxdby 返回的 base64 MIDI 并落盘播放。"""
        if not api_data.get("success"):
            self.show_error(player, f"下载失败：{api_data.get('message', '未知错误')}")
            return None
        data = api_data.get("data", {})
        midi_file = data.get("file")
        if not midi_file:
            self.show_error(player, "下载结果缺少音乐数据")
            return None
        safe_title = self.make_safe_song_name(data.get("title", fallback_title))
        midi_path = os.path.join(self.music_dir, safe_title + ".mid")
        seq_path = os.path.join(self.music_dir, safe_title + ".midseq")
        try:
            self.show_info(player, f"正在载入歌曲：§f「 §b{safe_title} §f」")
            with open(midi_path, "wb") as file:
                file.write(base64.b64decode(midi_file))
            self.midiplayer.translate_midi_to_seq_file(midi_path, seq_path)
        except (Exception, SystemExit) as err:
            self.print_err(f"处理下载的 MIDI 失败: {err}")
            self.show_error(player, f"载入失败：{err}")
            return None
        if not self.cfg["是否保留原始 MIDI"]:
            self.remove_file_quietly(midi_path)
        self.load_local_song_library()
        self.show_success(player, f"网络歌曲已载入：§f「 §b{safe_title} §f」")
        return safe_title


entry = plugin_entry(PersonalSongRequest)
