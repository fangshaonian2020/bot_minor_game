# main.py
# 少数胜（A/B）回合制游戏插件
# 流程：
# 1) 管理：/announce_game <group_id> <title>  在指定群宣布活动并设置群
# 2) 玩家：/register  在该群报名
# 3) 管理：/start_game [轮数]  默认5轮，开始第1轮，群内提示题目与选项，玩家去私聊发 A/B
# 4) 本轮过程中，玩家可多次修改，最后一次提交生效；管理可 /end_round 提前结算
# 5) 每轮结算：公布A/B人数与获胜方，加分，进入下一轮
# 6) 所有轮结束后：若最高分并列，自动延长赛，一轮一轮直到打破平分；最后公布总分

from __future__ import annotations
from typing import Dict, Set, Optional, List, Tuple
from dataclasses import dataclass, field
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

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

@register("minority_game", "YourName", "少数胜 A/B 回合制游戏", "1.0.0")
class MinorityGame(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.state = GameState()

    async def initialize(self):
        logger.info("[minority_game] 插件已加载")

    async def terminate(self):
        logger.info("[minority_game] 插件已卸载")

    # 工具：发送群消息
    async def send_group(self, group_id: int, text: str):
        # 若你的 SDK 发送接口不同，请替换为等价方法
        await self.context.bot.send_group_message(group_id, text)

    # 工具：发送私聊
    async def send_private(self, user_id: int, text: str):
        await self.context.bot.send_private_message(user_id, text)

    # 1) 管理：宣布活动并指定群
    @filter.command("announce_game")
    async def announce_game(self, event: AstrMessageEvent):
        """
        /announce_game <group_id> <title...>
        在指定群宣布活动、清空旧状态，等待玩家 /register
        """
        if not event.is_group:  # 建议群内发
            yield event.plain_result("请在任意处使用，参数里必须包含群号：/announce_game <群号> <标题>")
            return

        args = (event.message_str or "").strip().split(maxsplit=1)
        if len(args) < 1:
            yield event.plain_result("用法：/announce_game <群号> <标题，可选>")
            return

        try:
            gid = int(args[0])
        except Exception:
            yield event.plain_result("群号必须是数字：/announce_game <群号> <标题>")
            return

        title = args[1] if len(args) > 1 else "少数胜游戏"
        self.state = GameState(group_id=gid, title=title)
        await self.send_group(gid, f"【{title}】\n活动开始报名！请在本群发送 /register 报名参加。管理员可用 /start_game 开始游戏。")
        yield event.plain_result("已发布活动并重置状态。")

    # 2) 玩家报名
    @filter.command("register")
    async def register(self, event: AstrMessageEvent):
        """
        /register
        仅在活动群中有效。报名成功后系统会维护你的分数。
        """
        s = self.state
        if not s.group_id:
            yield event.plain_result("当前没有正在报名的活动。请等待管理员 /announce_game。")
            return

        if not event.is_group or event.get_group_id() != s.group_id:
            yield event.plain_result(f"请到目标群 {s.group_id} 内发送 /register 报名。")
            return

        uid = event.get_sender_id()
        s.registered.add(uid)
        s.scores.setdefault(uid, 0)
        yield event.plain_result("报名成功！等待管理员 /start_game。")

    # 2) 管理：开始游戏
    @filter.command("start_game")
    async def start_game(self, event: AstrMessageEvent):
        """
        /start_game [轮数]
        开始游戏，默认5轮
        """
        s = self.state
        if not s.group_id:
            yield event.plain_result("尚未发布活动。先用 /announce_game <群号> <标题>。")
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

        s.running = True
        s.round_index = 0
        s.overtime = False
        await self.send_group(s.group_id, f"【{s.title}】开始！本局共 {s.total_rounds} 轮。报名人数：{len(s.registered)}。")
        await self._start_next_round(event)

    # 3) 启动下一轮
    async def _start_next_round(self, event: AstrMessageEvent | None):
        s = self.state
        s.round_index += 1
        s.in_round = True
        s.choices.clear()

        round_type = "延长赛" if s.overtime else "第{}轮".format(s.round_index)
        prompt = f"{round_type}开始！\n规则：请私聊我发送 A 或 B 进行选择。\n少数方胜；若 A/B 持平，则奇数轮 A 胜，偶数轮 B 胜。\n请尽快完成提交，管理员可 /end_round 结算本轮。"
        await self.send_group(s.group_id, prompt)

        # 可选：也给所有报名者私聊提醒
        for uid in s.registered:
            try:
                await self.send_private(uid, f"[{self.state.title}] {round_type} 已开始，请发送 A 或 B 作为你的选择。可以重复修改，以最后一次为准。")
            except Exception as e:
                logger.debug(f"私聊提醒失败 uid={uid}: {e}")

    # 3) 玩家私聊提交 A/B
    @filter.command("A")
    async def choose_a(self, event: AstrMessageEvent):
        await self._handle_choice(event, "A")

    @filter.command("B")
    async def choose_b(self, event: AstrMessageEvent):
        await self._handle_choice(event, "B")

    async def _handle_choice(self, event: AstrMessageEvent, choice: str):
        s = self.state
        if not event.is_private:
            # 只允许私聊提交
            return

        if not s.running or not s.in_round:
            yield event.plain_result("当前不在提交阶段。")
            return

        uid = event.get_sender_id()
        if uid not in s.registered:
            yield event.plain_result("你尚未报名。请在活动群内发送 /register 报名。")
            return

        s.choices[uid] = choice
        yield event.plain_result(f"已记录你的选择：{choice}（可重复修改，以最后一次为准）")

    # 4) 管理：结束当前轮并结算
    @filter.command("end_round")
    async def end_round(self, event: AstrMessageEvent):
        s = self.state
        if not s.running or not s.in_round:
            yield event.plain_result("当前没有进行中的轮次。")
            return

        await self._settle_round(event)

        # 5) 进入下一轮或结束
        if not s.overtime and s.round_index >= s.total_rounds:
            # 正常轮打完，检查是否需要延长赛
            leaders, top = self._leaders()
            if len(leaders) >= 2:
                s.overtime = True
                await self.send_group(s.group_id, f"前 {s.total_rounds} 轮结束，最高分并列（{top} 分），进入延长赛！")
                await self._start_next_round(event)
            else:
                await self._finish_game(event)
        elif s.overtime:
            # 延长赛：若仍平分，继续；否则结束
            leaders, top = self._leaders()
            if len(leaders) >= 2:
                await self._start_next_round(event)
            else:
                await self._finish_game(event)
        else:
            await self._start_next_round(event)

    # 6) 管理：强制结束游戏
    @filter.command("end_game")
    async def end_game(self, event: AstrMessageEvent):
        s = self.state
        if not s.running:
            yield event.plain_result("没有进行中的游戏。")
            return
        if s.in_round:
            await self._settle_round(event)
        await self._finish_game(event)

    # 结算当前轮
    async def _settle_round(self, event: AstrMessageEvent):
        s = self.state
        s.in_round = False
        a = sum(1 for v in s.choices.values() if v == "A")
        b = sum(1 for v in s.choices.values() if v == "B")
        round_type = "延长赛" if s.overtime else f"第{s.round_index}轮"

        # 判定胜负
        if a == b:
            winner = "A" if s.round_index % 2 == 1 else "B"
            reason = f"人数相等，按轮次奇偶判定：{winner} 胜"
        elif a < b:
            winner, reason = "A", "少数方胜"
        else:
            winner, reason = "B", "少数方胜"

        # 给获胜方玩家加分
        winners: List[int] = [uid for uid, c in s.choices.items() if c == winner]
        for uid in winners:
            s.scores[uid] = s.scores.get(uid, 0) + 1

        # 公布本轮结果
        lines = [
            f"{round_type} 结算：",
            f"A 票数：{a} 人",
            f"B 票数：{b} 人",
            f"胜方：{winner}（{reason}）",
            f"本轮加分：胜方玩家 +1 分",
        ]
        await self.send_group(s.group_id, "\n".join(lines))

    # 结束游戏，公布总分
    async def _finish_game(self, event: AstrMessageEvent):
        s = self.state
        s.running = False
        s.in_round = False

        # 排行
        ranking = sorted(s.scores.items(), key=lambda kv: (-kv[1], kv[0]))
        if not ranking:
            await self.send_group(s.group_id, "本次游戏无人得分。")
        else:
            lines = [f"【{s.title}】最终结果"]
            for i, (uid, sc) in enumerate(ranking, 1):
                lines.append(f"{i}. 玩家{uid}：{sc} 分")
            await self.send_group(s.group_id, "\n".join(lines))

        # 清理本局状态但保留 group_id/title 以便复盘或再次 start
        gid, title = s.group_id, s.title
        self.state = GameState(group_id=gid, title=title)

    # 计算领先者
    def _leaders(self) -> Tuple[List[int], int]:
        s = self.state
        if not s.scores:
            return [], 0
        top = max(s.scores.values())
        leaders = [uid for uid, sc in s.scores.items() if sc == top]
        return leaders, top
