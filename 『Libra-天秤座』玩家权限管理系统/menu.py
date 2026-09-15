from __future__ import annotations

import datetime as _datetime
import queue
import threading
from typing import Any

try:
    from tooldelta import fmts
except ImportError:  # pragma: no cover
    fmts = None

try:
    from .core import menu_action, normalize_xuid, parse_flags
except ImportError:  # pragma: no cover
    from core import menu_action, normalize_xuid, parse_flags


class ConsoleMenuMixin:
    """Orion 风格同步控制台菜单；数字只在菜单循环内解释。"""

    _BORDER = "§d✧✦§f〓〓§b〓〓〓§9〓〓〓〓§1〓〓〓〓〓〓§9〓〓〓〓§b〓〓〓§f〓〓§d✦✧"

    def _orion(self, tag: str, message: str) -> None:
        marker = "§c❀" if tag in {"ALERT", "ERROR"} else "§6❀" if tag in {"WARN", "HELP"} else "§a❀"
        self._console_print(f"{marker} {message}")

    def _console_print(self, message: str) -> None:
        if fmts is not None:
            fmts.print_inf(message)
        else:
            self.print_inf(message)

    def _render_menu(self, menu: str) -> None:
        menus = {
            "main": ("总菜单", ("运行状态", "管理员列表", "设置权限", "快照管理", "实时管理")),
            "set": ("设置权限", ("管理员（11111111）", "成员（11111100）", "访客（00000000）", "自定义 8 位权限")),
            "snapshots": ("快照管理", ("新建快照", "还原快照", "删除快照")),
            "realtime": ("实时管理", ("运行详情", "启用或停用", "查看在线玩家权限", "管理托管玩家", "立即检查", "最近处理记录", "未授权管理员处理")),
        }
        title, items = menus.get(menu, menus["main"])
        self._console_print(self._BORDER)
        self._console_print(f"§l§d❐§f 『§6Libra-天秤座§f』 §b{title}§e管理 §d系统")
        for index, item in enumerate(items, 1):
            self._console_print(f"§l§b[ §e{index}§b ] §r§e{item}")
        self._console_print(self._BORDER)
        if menu == "main":
            self._console_print("§a❀ §b输入 §e[1-5]§b 之间的数字以选择功能，输入 §cq§b 退出")
        elif menu == "set":
            self._console_print("§a❀ §b输入 §e[1-4]§b 之间的数字以选择权限模板，输入 §e!§b 返回上一级，输入 §cq§b 退出")
        else:
            self._console_print("§a❀ §b输入 §e[1-3]§b 之间的数字，输入 §e!§b 返回上一级，输入 §cq§b 退出")

    def _open_menu(self) -> None:
        self._menu = "main"
        self._pending = None
        self._render_menu("main")

    def _close_menu(self) -> None:
        self._menu = "closed"
        self._pending = None
        self._orion("OK", "权限中心菜单已退出")

    def _input_timeout(self) -> float:
        try:
            return max(0.1, float(self.cfg.get("等待输入超时时间(秒)", 20)))
        except (TypeError, ValueError):
            return 20.0

    def _console_input(self, prompt: str) -> str | None:
        """带超时读取一行；超时通过 ``_input_timed_out`` 区分 EOF。"""
        self._input_timed_out = False
        if not prompt.startswith("§"):
            prompt = f"§a❀ §b{prompt}"
        rendered = fmts.fmt_info(prompt) if fmts is not None else prompt
        values: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def read_line() -> None:
            try:
                values.put(input(rendered).strip())
            except BaseException as exc:  # input 线程中的 EOF/中断
                values.put(exc)

        threading.Thread(target=read_line, name="libra-console-input", daemon=True).start()
        try:
            result = values.get(timeout=self._input_timeout())
        except queue.Empty:
            self._input_timed_out = True
            return None
        if isinstance(result, BaseException):
            return None
        return result

    def _finish_or_timeout(self) -> bool:
        value = self._console_input("输入任意字符继续，输入 q 退出：")
        if value is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            self._close_menu()
            return False
        if value.lower() == "q":
            self._close_menu()
            return False
        return True

    def _show_player_results(self, query: str) -> list[dict[str, str]]:
        results = self.search_players(query)
        if not results:
            self._orion("ALERT", f"没有找到与“{query}”匹配的玩家")
            return []
        self._console_print(self._BORDER)
        self._console_print("§a❀ §b已发现以下玩家名称与 XUID")
        for index, item in enumerate(results, 1):
            self._console_print(f"§l§b[ §e{index}§b ] §r§e{item['name']} - {item['xuid']}")
        self._console_print(self._BORDER)
        return results

    def _select_player_interactive(self, flags: str) -> bool:
        query = self._console_input("请输入玩家名称或 XUID（! 返回，q 退出）：")
        if query is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return False
        if query.lower() == "q":
            self._close_menu()
            return False
        if query in {"!", "！"}:
            return False
        try:
            results = [{"xuid": normalize_xuid(query), "name": query}]
        except ValueError:
            results = self._show_player_results(query)
        if not results:
            return False
        choice = self._console_input("请输入编号（! 返回，q 退出）：")
        if choice is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return False
        if choice.lower() == "q":
            self._close_menu()
            return False
        if choice in {"!", "！"}:
            return False
        if not choice.isdigit() or not 1 <= int(choice) <= len(results):
            self._orion("ALERT", "无效的玩家编号")
            return False
        target = results[int(choice) - 1]
        self._console_print(self._BORDER)
        normalized = target["xuid"].lower()
        managed = self.state.get("持续管理玩家", {}).get(normalized, False)
        if isinstance(managed, dict):
            managed = managed.get("是否启用", managed.get("启用", True))
        self._console_print(f"§a❀ §b将设置 §e{target['name']}§b（{target['xuid']}）为 §e{flags}")
        self._console_print("§a❀ §b[ §e1§b ] 仅设置本次权限")
        self._console_print("§a❀ §b[ §e2§b ] 设置并持续管理")
        if managed:
            self._console_print("§6❀ §b该玩家已启用持续管理，选择 1 将被拒绝；可先暂停管理")
        mode_choice = self._console_input("请输入设置方式（! 返回，q 退出）：")
        if mode_choice is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return False
        if mode_choice.lower() == "q":
            self._close_menu()
            return False
        if mode_choice in {"!", "！"}:
            return False
        if mode_choice not in {"1", "2"}:
            self._orion("ALERT", "无效的设置方式")
            return True
        management = "manage" if mode_choice == "2" else "once"
        self._console_print("§a❀ §b输入 §ey§b 确认，输入 §e!§b 返回，输入 §cq§b 退出")
        confirm = self._console_input("请输入确认：")
        if confirm is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return False
        if confirm.lower() == "q":
            self._close_menu()
            return False
        if confirm in {"!", "！"}:
            return False
        if confirm.lower() != "y":
            self._orion("WARN", "未确认，本次操作已取消")
            return True
        result = self.set_permission(target["xuid"], flags, actor="console", management=management)
        self._orion("OK" if result.get("success") else "ALERT", f"已设置 {target['xuid']} -> {flags}" if result.get("success") else result.get("message", "设置失败"))
        return True

    def _snapshot_page_size(self) -> int:
        try:
            return max(1, int(self.cfg.get("控制台菜单每页显示几项", 20)))
        except (TypeError, ValueError):
            return 20

    def _snapshot_detail(self, item: dict[str, Any]) -> None:
        snapshot = item["快照"]
        name = snapshot.get("名称") or snapshot.get("name") or "未命名快照"
        created = snapshot.get("创建时间") or snapshot.get("时间") or snapshot.get("time") or "未知"
        desired = snapshot.get("期望权限", snapshot.get("desired", {})) or {}
        admins = snapshot.get("已发现管理员", snapshot.get("observed_admins", [])) or []
        self._console_print(self._BORDER)
        self._console_print(f"§a❀ §b快照名称：§e{name}")
        self._console_print(f"§a❀ §b创建时间：§e{created}")
        self._console_print(f"§a❀ §b管理员数量：§e{len(admins)}")
        self._console_print(f"§a❀ §b期望权限：§e{len(desired)} 个")
        self._console_print(self._BORDER)

    def _choose_snapshot(self, operation: str) -> str:
        page = 1
        query = ""
        page_size = self._snapshot_page_size()
        while self._menu != "closed":
            items = self.list_snapshots(page=page, page_size=page_size, query=query)
            total = items.get("总数", 0)
            self._console_print(self._BORDER)
            title = "还原" if operation == "restore" else "删除"
            self._console_print(f"§l§d❐§f 『§6Libra-天秤座§f』 快照{title}列表 §7第 {page} 页")
            for index, item in enumerate(items.get("项目", []), 1):
                snapshot = item["快照"]
                name = snapshot.get("名称") or snapshot.get("name") or "未命名快照"
                desired = snapshot.get("期望权限", snapshot.get("desired", {})) or {}
                self._console_print(f"§l§b[ §e{index}§b ] §r§e{name} §7({len(desired)} 个权限)")
            self._console_print(self._BORDER)
            self._console_print("§a❀ §b输入序号选择，n 下一页，p 上一页，输入关键词查找，! 返回，q 退出")
            choice = self._console_input("请输入选项：")
            if choice is None:
                if self._input_timed_out:
                    self._orion("WARN", "输入超时")
                self._close_menu()
                return "closed"
            if choice.lower() == "q":
                self._close_menu()
                return "closed"
            if choice in {"!", "！"}:
                return "back"
            if choice.lower() == "n":
                if page * page_size < total:
                    page += 1
                continue
            if choice.lower() == "p":
                page = max(1, page - 1)
                continue
            entries = items.get("项目", [])
            if choice.isdigit() and 1 <= int(choice) <= len(entries):
                selected = entries[int(choice) - 1]
                self._snapshot_detail(selected)
                prompt = "确认还原此快照？输入 y 确认，! 返回，q 退出：" if operation == "restore" else "确认删除此快照？输入 y 确认，! 返回，q 退出："
                confirm = self._console_input(prompt)
                if confirm is None:
                    if self._input_timed_out:
                        self._orion("WARN", "输入超时")
                    self._close_menu()
                    return "closed"
                if confirm.lower() == "q":
                    self._close_menu()
                    return "closed"
                if confirm in {"!", "！"}:
                    continue
                if confirm.lower() == "y":
                    index = int(selected["索引"])
                    result = self.restore_snapshot(index, actor="console") if operation == "restore" else self.delete_snapshot(index, actor="console")
                    message = "快照还原完成" if operation == "restore" else "快照删除完成"
                    self._orion("OK" if result.get("success") else "ALERT", message if result.get("success") else result.get("error", "操作失败"))
                    return "done"
                self._orion("WARN", "未确认，本次操作已取消")
                return "done"
            query = choice.strip()
            page = 1
        return "closed"

    def _create_snapshot_interactive(self) -> bool:
        name = self._console_input("请输入快照名称，可留空：")
        if name is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return False
        if name.lower() == "q":
            self._close_menu()
            return False
        if name in {"!", "！"}:
            return True
        name = name.strip() or _datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        result = self.create_snapshot(name=name)
        self._orion("OK", f"快照已保存：{result.get('name', name)}")
        return True

    def _realtime_confirm(self, prompt: str) -> bool | None:
        value = self._console_input(prompt)
        if value is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return None
        if value.lower() == "q":
            self._close_menu()
            return None
        if value in {"!", "！"}:
            return False
        return value.lower() == "y"

    def _manage_realtime_player(self) -> None:
        query = self._console_input("请输入玩家名称或 XUID（! 返回，q 退出）：")
        if query is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return
        if query.lower() == "q":
            self._close_menu()
            return
        if query in {"!", "！"}:
            return
        try:
            normalized = normalize_xuid(query)
            target = {"xuid": normalized, "name": self.resolve_player_name(normalized) or query}
        except ValueError:
            matches = self._show_player_results(query)
            if not matches:
                return
            choice = self._console_input("请输入编号（! 返回，q 退出）：")
            if choice is None:
                if self._input_timed_out:
                    self._orion("WARN", "输入超时")
                return
            if choice.lower() == "q":
                self._close_menu()
                return
            if choice in {"!", "！"} or not choice.isdigit() or not 1 <= int(choice) <= len(matches):
                self._orion("ALERT", "无效的玩家编号")
                return
            target = matches[int(choice) - 1]
        flags = self._console_input("请输入 8 位目标权限（0/1）：")
        if flags is None:
            if self._input_timed_out:
                self._orion("WARN", "输入超时")
            return
        if flags.lower() == "q":
            self._close_menu()
            return
        try:
            parse_flags(flags)
        except ValueError as exc:
            self._orion("ALERT", str(exc))
            return
        self._console_print(f"§a❀ §b将为 §e{target['name']}§b 建立持续规则：§e{flags}")
        confirmed = self._realtime_confirm("请输入 y 确认，! 返回，q 退出：")
        if confirmed is True:
            result = self.realtime.set_managed_rule(target["xuid"], flags, actor="console")
            self._orion("OK" if result.get("success") else "ALERT", "持续管理规则已保存" if result.get("success") else result.get("message", "规则保存失败"))

    def _run_realtime_menu(self, choice: str) -> bool:
        if choice == "1":
            status = self.realtime.status()
            self._orion("OK", f"实时管理：{'运行中' if status['是否运行'] else '未运行'}，在线玩家 {status['在线玩家数']} 个，观测 {status['权限观测数']} 个（已就绪 {status['已就绪观测数']} 个），最近修正 {status['最近修正数量']} 条")
        elif choice == "2":
            current = self.realtime.enabled()
            confirmed = self._realtime_confirm(f"当前为{'启用' if current else '停用'}，确认切换？输入 y 确认：")
            if confirmed is True:
                status = self.realtime.set_enabled(not current)
                self._orion("OK", f"实时管理已{'启用' if status['是否启用'] else '停用'}")
        elif choice == "3":
            records = self.realtime.inspect_players()
            for record in records:
                name = record.get("玩家名称", record.get("玩家名", "未知玩家"))
                xuid = str(record.get("XUID", "")).lower()
                managed = self.state.get("持续管理玩家", {}).get(xuid, False)
                if isinstance(managed, dict):
                    managed = managed.get("是否启用", managed.get("启用", True))
                flags = record.get("实际权限")
                if not isinstance(flags, str) or len(flags) != 8 or any(c not in "01" for c in flags):
                    flags = "未知"
                    category = "未知"
                elif flags == "00000000":
                    category = "访客"
                elif flags == "11111100":
                    category = "成员"
                elif flags == "11111111":
                    category = "管理员"
                else:
                    category = "自定义"
                managed_text = "是" if managed else "否"
                self._orion("SCAN", f"{name}：{managed_text} {category} {flags}")
        elif choice == "4":
            self._manage_realtime_player()
        elif choice == "5":
            records = self.realtime.inspect_players()
            self._orion("SCAN", f"立即检查完成，共处理 {len(records)} 个在线玩家")
        elif choice == "6":
            rows = self.realtime._runtime().get("最近权限修正", [])[-self._snapshot_page_size():]
            if not rows:
                self._orion("OK", "暂无实时权限修正记录")
            for row in rows:
                self._orion("SCAN", f"{row.get('玩家名称', row.get('玩家XUID'))}：{row.get('实际权限')} -> {row.get('目标权限')}，{row.get('状态')}")
        elif choice == "7":
            self._console_print("§a❀ §b[ §e0§b ] 仅提醒  §b[ §e1§b ] 设为成员  §b[ §e2§b ] 设为访客")
            policy = self._console_input("请输入处理方式：")
            values = {"0": (0, "仅提醒"), "1": (1, "设为成员"), "2": (2, "设为访客")}
            if policy in values:
                number, label = values[policy]
                self.cfg.setdefault("实时管理", {})["未授权管理员处理(0:仅提醒,1:设为成员,2:设为访客)"] = number
                self._save_config()
                self._orion("OK", f"未授权管理员处理已设置为：{number}（{label}）")
            else:
                self._orion("ALERT", "无效的处理方式")
        else:
            self._orion("HELP", "请输入实时管理菜单中的数字序号")
        if self._menu != "closed":
            return self._finish_or_timeout()
        return False

    def _run_console_menu(self) -> None:
        self._menu = "main"
        self._pending = None
        while self._menu != "closed":
            self._render_menu(self._menu)
            choice = self._console_input("请输入选项：")
            if choice is None:
                if self._input_timed_out:
                    self._orion("WARN", "输入超时")
                self._close_menu()
                return
            if choice.lower() == "q":
                self._close_menu()
                return
            if choice in {"!", "！"}:
                if self._menu == "main":
                    self._close_menu()
                    return
                self._menu = "main"
                continue
            if self._menu == "main":
                action, _ = menu_action("main", choice)
                if action == "status":
                    mode = "魔法指令模式" if self._use_magic_command() else "普通指令模式"
                    self._orion("OK", f"缓存管理员 {len(self.state.get('已发现管理员', []))} 个，期望权限 {len(self.state.get('期望权限', {}))} 个，当前为{mode}")
                elif action == "list":
                    self._run_list()
                elif action == "set_menu":
                    self._menu = "set"
                    continue
                elif action == "snapshot_menu":
                    self._menu = "snapshots"
                    continue
                elif action == "realtime_menu":
                    self._menu = "realtime"
                    continue
                else:
                    self._orion("HELP", "请输入菜单中的数字序号")
                if not self._finish_or_timeout():
                    return
                continue
            if self._menu == "set":
                action, flags = menu_action("set", choice)
                if action in {"set_admin", "set_member", "set_guest"}:
                    self._select_player_interactive(flags or "00000000")
                elif action == "set_custom":
                    custom = self._console_input("请输入 8 位权限标志（0/1）：")
                    if custom is None:
                        if self._input_timed_out:
                            self._orion("WARN", "输入超时")
                        self._close_menu()
                        return
                    if custom.lower() == "q":
                        self._close_menu()
                        return
                    if custom not in {"!", "！"}:
                        try:
                            parse_flags(custom)
                            self._select_player_interactive(custom)
                        except ValueError as exc:
                            self._orion("ALERT", str(exc))
                else:
                    self._orion("HELP", "请选择 1-4，或输入 ! 返回")
                if self._menu != "closed" and not self._finish_or_timeout():
                    return
                continue
            if self._menu == "snapshots":
                action, _ = menu_action("snapshots", choice)
                if action == "snapshot_create":
                    self._create_snapshot_interactive()
                    if self._menu != "closed" and not self._finish_or_timeout():
                        return
                elif action in {"snapshot_restore", "snapshot_delete"}:
                    result = self._choose_snapshot("restore" if action == "snapshot_restore" else "delete")
                    if result == "done" and not self._finish_or_timeout():
                        return
                    if result == "closed":
                        return
                else:
                    self._orion("HELP", "请选择 1-3，或输入 ! 返回")
                continue
            if self._menu == "realtime":
                if self._run_realtime_menu(choice):
                    continue
                return

    def on_menu_token(self, token: str, args: list[str] | None = None) -> None:
        if self._menu == "closed":
            return
        if token.lower() == "q":
            self._close_menu()
        elif token in {"!", "！"}:
            self._menu = "main" if self._menu != "main" else "closed"
        elif self._menu == "main" and token == "4":
            self._menu = "snapshots"
        elif self._menu == "main" and token == "5":
            self._menu = "realtime"

    def on_console(self, args: list[str]) -> None:
        self._run_console_menu()
