# main.py
# 少数胜（A/B）回合制游戏插件
# v1.0.6
# - 修复 async_generator 误用 await
# - /announce_game 无参，群内开局
# - 报名命令：/mg_register、/join、/报名、/参加（并保留 /register）
# - 支持“私聊报名→群内广播人数”，尽量兼容多种 OneBot 发送 API

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List, Tuple, Callable, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


# —— 工具：判断群/私聊 ——
def evt_group_id(event: AstrMessageEvent) -> Optional[int]:
    try:
        return event.get_group_id()
    except Exception:
        return None

def is_group_event(event: AstrMessageEvent) -> bool:
    gid = evt_group_id(event)
    return gid is not None and gid != 0

def is_private_event(event: AstrMessageEvent) -> bool:
    return not is_group_event(event)


# —— 状态模型 ——
@dataclass
class GameState:
    group_id: Optional[int] = None
    title: str = "少数胜游戏"
    registered: Set[int] = field(default_factory=set)
    running: bool = False
    round_index: int = 0  # 从1开始
    total_rounds: int = 5
    in_round: bool = False
    choices: Dict[int, str] = field(default_factory=dict)  # user_id -> "A"/"B"
    scores: Dict[int, int] = field(default_factory=dict)   # user_id -> score
    overtime: bool = False  # 是否处于延长赛模式


@register("minor_game", "YourName", "少数胜 A/B 回合制游戏", "1.0.6")
class MinorGame(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.state = GameState()

    async def initialize(self):
        logger.info("[minor_game] 插件已加载")

    async def terminate(self):
        logger.info("[minor_game] 插件已卸载")

    # —— 兼容性强的“群发文本” —— 从私聊场景向群里广播
    async def send_group_text(self, group_id: int, text: str) -> bool:
        bot = getattr(self.context, "bot", None)
        if bot is None:
            return False

        async def try_call(obj: Any, name: str, *args, **kwargs) -> bool:
            fn = getattr(obj, name, None)
            if callable(fn):
                try:
                    await fn(*args, **kwargs)
                    return True
                except Exception as e:
                    logger.debug(f"[minor_game] send_group_text try {name} failed: {e}")
            return False

        # 1) 常见方法名（优先使用关键词参数，兼容 OneBot v11）
        if await try_call(bot, "send_group_msg", group_id=group_id, message=text): return True
        if await try_call(bot, "send_group_message", group_id, text): return True
        # 2) 更通用的 send_message / send_msg
        if await try_call(bot, "send_message", message_type="group", group_id=group_id, message=text): return True
        if await try_call(bot, "send_msg", message_type="group", group_id=group_id, message=text): return True
        # 3) call_api 形式
        call_api = getattr(bot, "call_api", None)
        if callable(call_api):
            try:
                await call_api("send_group_msg", group_id=group_id, message=text)
                return True
            except Exception as e:
                logger.debug(f"[minor_game] call_api send_group_msg failed: {e}")
            try:
                await call_api("send_msg", message_type="group", group_id=group_id, message=text)
                return True
            except Exception as e:
                logger.debug(f"[minor_game] call_api send_msg failed: {e}")
        # 4) 有些实现把 API 挂在 bot.api 下
        api = getattr(bot, "api", None)
        if api:
            if await try_call(api, "send_group_msg", group_id=group_id, message=text): return True
            if await try_call(api, "send_msg", message_type="group", group_id=group_id, message=text): return True

        logger.warning("[minor_game] 未找到可用的群发接口，已跳过群内广播。")
        return False

    # —— 1) 管理：宣布活动（无参数，群内使用） ——
    @filter.command("announce_game")
    async def announce_game(self, event: AstrMessageEvent):
        """
        /announce_game
        在“当前群”宣布活动、清空旧状态，等待玩家报名
        """
        if not is_group_event(event):
            yield event.plain_result("请在目标群内发送 /announce_game。")
            return

        gid = evt_group_id(event)
        title = "少数胜游戏"

        self.state = GameState(group_id=gid, title=title)
        yield event.plain_result(
            f"【{title}】\n活动开始报名！请在本群发送 /mg_register 或 /join（也可 /报名、/参加）报名参加。\n"
            f"管理员可用 /start_game 开始游戏（默认 5 轮，也可 /start_game 7 指定轮数）。\n"
            f"每轮请私聊我发送 /A 或 /B；少数方胜，平票则奇数轮 A 胜、偶数轮 B 胜。"
        )

    # —— 2) 玩家报名（支持群内/私聊触发；私聊时尝试在群里广播人数） ——
    async def _register_impl(self, event: AstrMessageEvent):
        s = self.state
        if not s.group_id:
            yield event.plain_result("当前没有正在报名的活动。请等待管理员 /announce_game。")
            return

        uid = event.get_sender_id()
        s.registered.add(uid)
        s.scores.setdefault(uid, 0)

        # 自身会话确认
        yield event.plain_result("报名成功！等待管理员 /start_game。")

        # 群内广播报名人数（若当前就是活动群，直接在本群回；若是私聊则尝试 send_group_text）
        name = ""
        try:
            name = event.get_sender_name()
        except Exception:
            name = str(uid)

        text = f"报名更新：{name} 已报名（当前 {len(s.registered)} 人）。"
        if is_group_event(event) and evt_group_id(event) == s.group_id:
            # 直接在当前群回复
            yield event.plain_result(text)
        else:
            # 私聊报名 → 群里广播
            await self.send_group_text(s.group_id, text)

    # ——— 多个报名命令别名（避免 /register 冲突） ———
    @filter.command("mg_register")
    async def mg_register(self, event: AstrMessageEvent):
        async for res in self._register_impl(event):
            yield res

    @filter.command("join")
    async def join(self, event: AstrMessageEvent):
        async for res in self._register_impl(event):
            yield res

    @filter.command("报名")
    async def join_cn1(self, event: AstrMessageEvent):
        async for res in self._register_impl(event):
            yield res

    @filter.command("参加")
    async def join_cn2(self, event: AstrMessageEvent):
        async for res in self._register_impl(event):
            yield res

    # 仍保留 /register（若被其他插件吞掉，请使用上面的别名）
    @filter.command("register")
    async def register(self, event: AstrMessageEvent):
        async for res in self._register_impl(event):
            yield res

    # —— 2) 管理：开始游戏 ——
    @filter.command("start_game")
    async def start_game(self, event: AstrMessageEvent):
        """
        /start_game [轮数]
        开始游戏，默认 5 轮
        """
        s = self.state
        if not s.group_id:
            yield event.plain_result("尚未发布活动。请先在群内 /announce_game。")
            return
        if not is_group_event(event) or evt_group_id(event) != s.group_id:
            yield event.plain_result("请在活动群内使用 /start_game。")
            return
        if s.running:
            yield event.plain_result("游戏已在进行中。")
            return
        if len(s.registered) < 1:
            yield event.plain_result("还没有报名的玩家。")
            return

        parts = (event.message_str or "").strip().split()
        if len(parts) >= 1 and parts[0].isdigit():
            s.total_rounds = int(parts[0])
        else:
            s.total_rounds = 5

        s.running = True
        s.round_index = 0
        s.overtime = False

        yield event.plain_result(
            f"【{s.title}】开始！本局共 {s.total_rounds} 轮；报名人数：{len(s.registered)}。\n"
            f"现在开始第 1 轮：请所有玩家“私聊我”发送 /A 或 /B。"
        )
        async for res in self._start_next_round(event):
            yield res

    # —— 3) 启动下一轮（在群内提示） ——
    async def _start_next_round(self, event: AstrMessageEvent):
        s = self.state
        s.round_index += 1
        s.in_round = True
        s.choices.clear()

        round_type = "延长赛" if s.overtime else f"第{s.round_index}轮"
        prompt = (
            f"{round_type}开始！\n"
            "规则：请“私聊我”发送 /A 或 /B（大小写均可）。\n"
            "少数方胜；若 A/B 持平，则奇数轮 A 胜，偶数轮 B 胜。\n"
            "管理员可 /end_round 结算本轮。"
        )
        yield event.plain_result(prompt)

    # —— 3) 玩家私聊提交 A/B（支持大小写） ——
    @filter.command("A")
    async def choose_A(self, event: AstrMessageEvent):
        async for res in self._handle_choice(event, "A"):
            yield res

    @filter.command("a")
    async def choose_a(self, event: AstrMessageEvent):
        async for res in self._handle_choice(event, "A"):
            yield res

    @filter.command("B")
    async def choose_B(self, event: AstrMessageEvent):
        async for res in self._handle_choice(event, "B"):
            yield res

    @filter.command("b")
    async def choose_b(self, event: AstrMessageEvent):
        async for res in self._handle_choice(event, "B"):
            yield res

    async def _handle_choice(self, event: AstrMessageEvent, choice: str):
        s = self.state
        if not is_private_event(event):
            return  # 只允许私聊提交

        if not s.running or not s.in_round:
            yield event.plain_result("当前不在提交阶段。")
            return

        uid = event.get_sender_id()
        if uid not in s.registered:
            yield event.plain_result("你尚未报名。请回到活动群内发送 /mg_register 或 /join。")
            return

        s.choices[uid] = choice
        yield event.plain_result(f"已记录你的选择：{choice}（可重复修改，以最后一次为准）")

    # —— 4) 管理：结束当前轮并结算 —— 
    @filter.command("end_round")
    async def end_round(self, event: AstrMessageEvent):
        s = self.state
        if not s.running or not s.in_round:
            yield event.plain_result("当前没有进行中的轮次。")
            return
        if not is_group_event(event) or evt_group_id(event) != s.group_id:
            yield event.plain_result("请在活动群内使用 /end_round。")
            return

        a, b, winner, reason = self._settle_round_logic()
        s.in_round = False

        lines = [
            f"{'延长赛' if s.overtime else f'第{s.round_index}轮'} 结算：",
            f"A 票数：{a} 人",
            f"B 票数：{b} 人",
            f"胜方：{winner}（{reason}）",
            f"本轮加分：胜方玩家 +1 分",
        ]
        yield event.plain_result("\n".join(lines))

        # —— 5) 进入下一轮或结束 ——
        if not s.overtime and s.round_index >= s.total_rounds:
            leaders, top = self._leaders()
            if len(leaders) >= 2:
                s.overtime = True
                yield event.plain_result(f"前 {s.total_rounds} 轮结束，最高分并列（{top} 分），进入延长赛！")
                async for res in self._start_next_round(event):
                    yield res
            else:
                async for res in self._finish_game(event):
                    yield res
        elif s.overtime:
            leaders, _ = self._leaders()
            if len(leaders) >= 2:
                async for res in self._start_next_round(event):
                    yield res
            else:
                async for res in self._finish_game(event):
                    yield res
        else:
            async for res in self._start_next_round(event):
                yield res

    # —— 6) 管理：强制结束游戏 —— 
    @filter.command("end_game")
    async def end_game(self, event: AstrMessageEvent):
        s = self.state
        if not s.running:
            yield event.plain_result("没有进行中的游戏。")
            return
        if not is_group_event(event) or evt_group_id(event) != s.group_id:
            yield event.plain_result("请在活动群内使用 /end_game。")
            return

        if s.in_round:
            a, b, winner, reason = self._settle_round_logic()
            s.in_round = False
            lines = [
                f"{'延长赛' if s.overtime else f'第{s.round_index}轮'} 结算：",
                f"A 票数：{a} 人",
                f"B 票数：{b} 人",
                f"胜方：{winner}（{reason}）",
                f"本轮加分：胜方玩家 +1 分",
            ]
            yield event.plain_result("\n".join(lines))

        async for res in self._finish_game(event):
            yield res

    # —— 轮次结算逻辑（不直接发送消息） ——
    def _settle_round_logic(self) -> Tuple[int, int, str, str]:
        s = self.state
        a = sum(1 for v in s.choices.values() if v == "A")
        b = sum(1 for v in s.choices.values() if v == "B")

        if a == b:
            winner = "A" if s.round_index % 2 == 1 else "B"
            reason = f"人数相等，按轮次奇偶判定：{winner} 胜"
        elif a < b:
            winner, reason = "A", "少数方胜"
        else:
            winner, reason = "B", "少数方胜"

        winners: List[int] = [uid for uid, c in self.state.choices.items() if c == winner]
        for uid in winners:
            self.state.scores[uid] = self.state.scores.get(uid, 0) + 1

        return a, b, winner, reason

    # —— 结束游戏，公布总分（在群内回复） ——
    async def _finish_game(self, event: AstrMessageEvent):
        s = self.state
        s.running = False
        s.in_round = False

        ranking = sorted(s.scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if not ranking:
            yield event.plain_result("本次游戏无人得分。")
        else:
            lines = [f"【{s.title}】最终结果"]
            for i, (uid, sc) in enumerate(ranking, 1):
                lines.append(f"{i}. 玩家{uid}：{sc} 分")
            yield event.plain_result("\n".join(lines))

        gid, title = s.group_id, s.title
        self.state = GameState(group_id=gid, title=title)

    # —— 计算领先者 —— 
    def _leaders(self) -> Tuple[List[int], int]:
        s = self.state
        if not s.scores:
            return [], 0
        top = max(s.scores.values())
        leaders = [uid for uid, sc in s.scores.items() if sc == top]
        return leaders, top
