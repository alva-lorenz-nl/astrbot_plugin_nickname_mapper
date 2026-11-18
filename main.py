import asyncio
import json
import os
import tempfile
from typing import Dict, List, Optional, Tuple
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools


@register(
    "astrbot_plugin_nickname_mapper",
    "Alva",
    "用户昵称映射，支持按独立对话(conversation_id)设置个性化昵称，只影响 LLM 接收的内容",
    "1.1.1",
)
class NicknameMapperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        DATA_DIR = StarTools.get_data_dir()
        self.CONFIG_FILE = os.path.join(DATA_DIR, "nicknames.json")

        # 默认配置
        self.enabled = config.get("enabled", True)

        # 数据结构: { "(user_id,session_id,conversation_id)": "custom_nickname" }
        self.nickname_map: Dict[str, str] = {}
        self.lock = asyncio.Lock()

        self._load_nicknames()

    async def initialize(self):
        pass

    async def terminate(self):
        pass

    def _get_conv_key(self, user_id: str, session_id: str, conv_id: str) -> str:
        """生成带独立对话 ID 的复合键: (user_id,session_id,conv_id)"""
        return f"({user_id},{session_id},{conv_id})"

    def _get_session_key(self, user_id: str, session_id: str) -> str:
        """生成会话级复合键: (user_id,session_id)"""
        return f"({user_id},{session_id})"

    def _parse_key(self, key: str) -> Tuple[str, str, str]:
        """解析键，返回 (user_id, session_id, conv_id)"""
        if key.startswith("(") and key.endswith(")"):
            inner = key[1:-1]
            parts = inner.split(",")
            if len(parts) == 3:
                return parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                return parts[0], parts[1], ""
        return "", key, ""

    async def _ensure_curr_conv_id(self, event: AstrMessageEvent) -> str:
        """获取当前独立对话 ID，若不存在则自动创建或返回空字符串"""
        umo = str(event.unified_msg_origin or event.get_session_id() or "")
        if not umo:
            return ""
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            return ""
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            return str(cid or "")
        except Exception as e:
            logger.warning(f"获取当前对话 ID 失败: {e}")
            return ""

    async def _get_curr_conv_id_readonly(self, event: AstrMessageEvent) -> str:
        """只读获取当前独立对话 ID"""
        umo = str(event.unified_msg_origin or event.get_session_id() or "")
        if not umo:
            return ""
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            return ""
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            return str(cid or "")
        except Exception:
            return ""

    async def _get_session_conv_titles(self, umo: str) -> Dict[str, str]:
        """获取当前会话下所有存在对话的 {cid: title} 映射"""
        titles: Dict[str, str] = {}
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr or not umo:
            return titles
        try:
            convs = await conv_mgr.get_conversations(umo)
            for c in convs:
                cid = getattr(c, "cid", None)
                if cid:
                    titles[str(cid)] = getattr(c, "title", None) or "新对话"
        except Exception as e:
            logger.warning(f"获取会话对话列表失败: {e}")
        return titles

    def _get_session_entries(
        self, user_id: str, session_id: str
    ) -> List[Tuple[str, str, str]]:
        """获取当前用户在当前会话下的所有昵称配置项: [(raw_key, conv_id, nickname), ...]"""
        entries: List[Tuple[str, str, str]] = []
        for k, nick in self.nickname_map.items():
            u_id, s_id, c_id = self._parse_key(k)
            if s_id == session_id and (not u_id or u_id == user_id):
                entries.append((k, c_id, nick))
        return entries

    def _load_nicknames(self):
        """从文件加载昵称映射"""
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.nickname_map = json.load(f)
                logger.info(f"已加载 {len(self.nickname_map)} 条昵称映射")
            except Exception as e:
                logger.error(f"加载昵称映射失败: {e}")

    def _save_nicknames(self):
        """保存昵称映射到文件"""
        try:
            dirpath = os.path.dirname(self.CONFIG_FILE)
            os.makedirs(dirpath, exist_ok=True)
            fd, tmppath = tempfile.mkstemp(dir=dirpath, prefix="nick-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.nickname_map, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmppath, self.CONFIG_FILE)
        except Exception as e:
            logger.error(f"保存昵称映射失败: {e}")

    @filter.command("设置昵称")
    async def set_nickname(self, event: AstrMessageEvent):
        """设置当前独立对话的昵称"""
        if not self.enabled:
            yield event.plain_result("昵称映射功能已禁用")
            return

        content = (event.get_message_str() or "").strip()
        parts = content.split(maxsplit=1)

        if len(parts) < 2:
            yield event.plain_result("请使用: /设置昵称 <昵称>")
            return

        nickname = parts[1].strip()
        if not nickname:
            yield event.plain_result("昵称不能为空")
            return

        user_id = str(event.get_sender_id() or "")
        session_id = str(event.unified_msg_origin or event.get_session_id() or "")
        if not user_id or not session_id:
            yield event.plain_result("无法获取用户信息")
            return

        conv_id = await self._ensure_curr_conv_id(event)
        if conv_id:
            primary_key = self._get_conv_key(user_id, session_id, conv_id)
        else:
            primary_key = self._get_session_key(user_id, session_id)

        async with self.lock:
            self.nickname_map[primary_key] = nickname
            self._save_nicknames()

        short_cid = conv_id[:4] if conv_id else "默认会话"
        yield event.plain_result(f"✓ 当前对话({short_cid})昵称已设置为: {nickname}")
        logger.info(f"用户 {primary_key} 设置昵称: {nickname}")

    @filter.command("查看昵称")
    async def get_nickname(self, event: AstrMessageEvent):
        """查看当前独立对话的昵称，并列出当前会话下所有对话的昵称记录"""
        if not self.enabled:
            yield event.plain_result("昵称映射功能已禁用")
            return

        user_id = str(event.get_sender_id() or "")
        session_id = str(event.unified_msg_origin or event.get_session_id() or "")
        if not user_id or not session_id:
            yield event.plain_result("无法获取用户信息")
            return

        curr_cid = await self._get_curr_conv_id_readonly(event)
        titles = await self._get_session_conv_titles(session_id)
        curr_nick = await self._get_nickname_for_event(event)

        short_curr = curr_cid[:4] if curr_cid else "无"
        curr_title = titles.get(curr_cid, "新对话") if curr_cid else "未创建对话"

        lines = []
        if curr_nick:
            lines.append(f"当前对话 [{curr_title}({short_curr})] 昵称: {curr_nick}")
        else:
            lines.append(f"当前对话 [{curr_title}({short_curr})] 尚未设置专属昵称")

        entries = self._get_session_entries(user_id, session_id)
        if entries:
            lines.append("")
            lines.append("📋 当前会话已设置的对话昵称列表：")
            for idx, (_, c_id, nick) in enumerate(entries, start=1):
                if c_id:
                    short_id = c_id[:4]
                    if c_id in titles:
                        status_tag = " [当前]" if c_id == curr_cid else ""
                        title_str = f"{titles[c_id]}({short_id}){status_tag}"
                    else:
                        title_str = f"已删除对话({short_id})"
                    lines.append(f"{idx}. {title_str} → {nick}  (ID: {short_id})")
                else:
                    lines.append(f"{idx}. 会话默认(旧版) → {nick}  (ID: default)")
            lines.append("")
            lines.append("💡 提示: 可用「/删除昵称」删当前对话，或「/删除昵称 <ID/序号>」清理指定记录")

        yield event.plain_result(chr(10).join(lines))

    @filter.command("昵称列表")
    async def list_nicknames(self, event: AstrMessageEvent):
        """别名：等价于 /查看昵称，仅为习惯该叫法的用户保留"""
        async for res in self.get_nickname(event):
            yield res

    @filter.command("删除昵称")
    async def delete_nickname(self, event: AstrMessageEvent):
        """删除当前对话的昵称，或通过对话ID(前4位/完整ID/序号/all)删除指定对话昵称"""
        if not self.enabled:
            yield event.plain_result("昵称映射功能已禁用")
            return

        user_id = str(event.get_sender_id() or "")
        session_id = str(event.unified_msg_origin or event.get_session_id() or "")
        if not user_id or not session_id:
            yield event.plain_result("无法获取用户信息")
            return

        content = (event.get_message_str() or "").strip()
        parts = content.split(maxsplit=1)
        target_arg = parts[1].strip() if len(parts) >= 2 else ""

        curr_cid = await self._get_curr_conv_id_readonly(event)
        entries = self._get_session_entries(user_id, session_id)

        if not entries:
            yield event.plain_result("当前会话没有任何已设置的昵称记录")
            return

        keys_to_delete: List[Tuple[str, str, str]] = []

        if not target_arg:
            for raw_k, c_id, nick in entries:
                if curr_cid and c_id == curr_cid:
                    keys_to_delete.append((raw_k, c_id, nick))
                elif not curr_cid and not c_id:
                    keys_to_delete.append((raw_k, c_id, nick))
            if not keys_to_delete:
                short_curr = curr_cid[:4] if curr_cid else "无"
                msg = chr(10).join([
                    f"当前对话({short_curr})没有设置昵称。",
                    "如需删除其他对话的昵称，请发送「/查看昵称」获取 ID 后使用：/删除昵称 <ID或序号>"
                ])
                yield event.plain_result(msg)
                return
        elif target_arg.lower() in ("all", "全部", "所有"):
            keys_to_delete = list(entries)
        elif target_arg.lower() in ("default", "默认"):
            for raw_k, c_id, nick in entries:
                if not c_id:
                    keys_to_delete.append((raw_k, c_id, nick))
        else:
            for raw_k, c_id, nick in entries:
                if c_id and (c_id == target_arg or c_id.startswith(target_arg)):
                    keys_to_delete.append((raw_k, c_id, nick))
            if not keys_to_delete and target_arg.isdigit():
                idx = int(target_arg)
                if 1 <= idx <= len(entries):
                    keys_to_delete.append(entries[idx - 1])

            if not keys_to_delete:
                yield event.plain_result(
                    f"未找到匹配「{target_arg}」的对话昵称记录，请先发送「/查看昵称」核对 ID 或序号。"
                )
                return

        async with self.lock:
            for raw_k, _, _ in keys_to_delete:
                self.nickname_map.pop(raw_k, None)
            self._save_nicknames()

        deleted_desc = []
        for _, c_id, nick in keys_to_delete:
            short_id = c_id[:4] if c_id else "默认"
            deleted_desc.append(f"{short_id}({nick})")

        joined_desc = ", ".join(deleted_desc)
        yield event.plain_result(f"✓ 已删除昵称记录: {joined_desc}")
        logger.info(f"用户 ({user_id},{session_id}) 删除了昵称记录: {deleted_desc}")

    async def _get_nickname_for_event(self, event: AstrMessageEvent) -> Optional[str]:
        """获取指定事件当前独立对话的昵称"""
        user_id = str(event.get_sender_id() or "")
        session_id = str(event.unified_msg_origin or event.get_session_id() or "")
        if not session_id:
            return None

        curr_cid = await self._get_curr_conv_id_readonly(event)
        if user_id and curr_cid:
            conv_key = self._get_conv_key(user_id, session_id, curr_cid)
            if conv_key in self.nickname_map:
                return self.nickname_map[conv_key]

        # 兼容未带 conversation_id 的旧版回退键（若存在）
        if user_id:
            sess_key = self._get_session_key(user_id, session_id)
            if sess_key in self.nickname_map:
                return self.nickname_map[sess_key]
        if session_id in self.nickname_map:
            return self.nickname_map[session_id]
        return None

    @filter.on_llm_request(priority=5)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求时替换用户身份"""
        if not self.enabled:
            return

        nickname = await self._get_nickname_for_event(event)
        if not nickname:
            return

        if getattr(event, "message_obj", None) and getattr(
            event.message_obj, "sender", None
        ):
            event.message_obj.sender.nickname = nickname

        replaced = False

        extra_parts = getattr(req, "extra_user_content_parts", None)
        if extra_parts:
            for part in extra_parts:
                text = getattr(part, "text", None)
                if isinstance(text, str) and "Nickname:" in text:
                    new_lines = []
                    for line in text.splitlines():
                        if "User ID:" in line and "Nickname:" in line:
                            prefix, rest = line.split("Nickname:", 1)
                            if "</system_reminder>" in rest:
                                line = f"{prefix}Nickname: {nickname}</system_reminder>"
                            else:
                                line = f"{prefix}Nickname: {nickname}"
                            replaced = True
                        new_lines.append(line)
                    part.text = chr(10).join(new_lines)

        if req.prompt:
            if "User ID:" in req.prompt and "Nickname:" in req.prompt:
                p_lines = []
                for line in req.prompt.splitlines():
                    if "User ID:" in line and "Nickname:" in line:
                        prefix, rest = line.split("Nickname:", 1)
                        if "]" in rest:
                            _, after_bracket = rest.split("]", 1)
                            line = f"{prefix}Nickname: {nickname}]{after_bracket}"
                        else:
                            line = f"{prefix}Nickname: {nickname}"
                        replaced = True
                    p_lines.append(line)
                req.prompt = chr(10).join(p_lines)
            elif not replaced:
                user_id = event.get_sender_id() or event.unified_msg_origin
                req.prompt = f"[User ID: {user_id}, Nickname: {nickname}]" + chr(10) + req.prompt.strip()

        logger.debug(f"已将用户 {event.unified_msg_origin} 的身份替换为: {nickname}")
