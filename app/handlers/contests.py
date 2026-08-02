"""Contests menu: shows the lowest-id active referral contest with leaderboard."""

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral_contest import (
    get_contest_leaderboard,
    get_contest_leaderboard_with_virtual,
    get_referrer_score,
    list_virtual_participants,
)
from app.database.models import ReferralContest
from app.localization.texts import get_texts
from app.utils.decorators import auth_required, error_handler


logger = structlog.get_logger(__name__)


def _normalize_end(end_at: datetime) -> datetime:
    """Treat midnight end_at as end-of-day, matching service/CRUD behavior."""
    if end_at.hour == 0 and end_at.minute == 0 and end_at.second == 0:
        return end_at.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end_at


def _get_contest_tz(contest: ReferralContest) -> ZoneInfo:
    tz_name = contest.timezone or settings.TIMEZONE or 'UTC'
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo('UTC')


def _fmt_local(dt_value: datetime, tz: ZoneInfo) -> str:
    base = dt_value if dt_value.tzinfo else dt_value.replace(tzinfo=UTC)
    return base.astimezone(tz).strftime('%d.%m.%Y %H:%M')


def _fmt_remaining(end_utc: datetime, now_utc: datetime) -> str:
    delta = end_utc - now_utc
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return '0 мин'

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts: list[str] = []
    if days:
        parts.append(f'{days} дн')
    if hours:
        parts.append(f'{hours} ч')
    if minutes and not days:
        parts.append(f'{minutes} мин')
    return ' '.join(parts) or '< 1 мин'


async def _compute_user_rank(db: AsyncSession, contest_id: int, user_id: int) -> int | None:
    """Rank of user in merged leaderboard (real + virtual), matching service sort order."""
    real_lb = await get_contest_leaderboard(db, contest_id)
    virtual = await list_virtual_participants(db, contest_id)

    entries: list[tuple[int, int, int]] = [(user.id, score, amount) for user, score, amount in real_lb]
    entries.extend((-1, vp.referral_count, vp.total_amount_kopeks) for vp in virtual)
    entries.sort(key=lambda x: (-x[1], -x[2]))

    for idx, (uid, _score, _amount) in enumerate(entries, start=1):
        if uid == user_id:
            return idx
    return None


async def _get_current_contest(db: AsyncSession) -> ReferralContest | None:
    """Return the lowest-id active contest whose period covers now_utc."""
    now_utc = datetime.now(UTC)
    result = await db.execute(
        select(ReferralContest)
        .where(
            and_(
                ReferralContest.is_active.is_(True),
                ReferralContest.start_at <= now_utc,
            )
        )
        .order_by(ReferralContest.id.asc())
    )
    for contest in result.scalars().all():
        if _normalize_end(contest.end_at) >= now_utc:
            return contest
    return None


def _build_keyboard(has_contest: bool, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[types.InlineKeyboardButton]] = []
    if has_contest and settings.is_referral_program_enabled():
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('CONTEST_INVITE_FRIENDS_BTN', '👥 Пригласить друзей'),
                    callback_data='menu_referrals',
                )
            ]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('CONTEST_REFRESH_BTN', '🔄 Обновить'),
                callback_data='contests_menu',
            )
        ]
    )
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _rule_short(contest_type: str) -> str:
    """One-liner: what counts as +1 score. Used inside the info block."""
    contest_type = (contest_type or '').strip()
    if contest_type == 'referral_registered':
        return 'за каждого друга, пришедшего по ссылке'
    if contest_type == 'referral_paid':
        return 'за каждого друга, купившего подписку'
    return 'за активность друзей по вашей ссылке'


def _how_to_play(contest_type: str) -> str:
    """Action-oriented 3-step guide for the contest card."""
    contest_type = (contest_type or '').strip()
    if contest_type == 'referral_paid':
        step_three = (
            '3. Зачёт идёт, только когда друг <b>оплатит подписку</b> '
            '<i>(одной регистрации мало)</i>.'
        )
    else:
        step_three = '3. Каждый, кто перейдёт по ней, даёт вам <b>+1 зачёт</b>.'
    return (
        '💡 <b>Как играть</b>\n'
        '1. Нажмите «👥 Пригласить друзей».\n'
        '2. Скопируйте свою ссылку.\n'
        f'{step_three}\n'
        '🏆 В призы попадает <b>топ-3</b>.'
    )


def _rank_prefix(idx: int) -> str:
    if idx == 1:
        return '🥇'
    if idx == 2:
        return '🥈'
    if idx == 3:
        return '🥉'
    return f'<b>{idx}.</b>'


def _build_contest_text(
    contest: ReferralContest,
    leaderboard: list[tuple[str, int, int, bool]],
    user_rank: int | None,
    user_score: int,
    gap_to_prize: int | None,
    now_utc: datetime,
) -> str:
    tz = _get_contest_tz(contest)
    end_normalized = _normalize_end(contest.end_at)

    header = f'🏆 <b>{html.escape(contest.title)}</b>'

    info_lines: list[str] = []
    if contest.description:
        info_lines.append(html.escape(contest.description))
    if contest.prize_text:
        info_lines.append(f'🎁 <b>Приз:</b> {html.escape(contest.prize_text)} <i>(топ-3)</i>')
    info_lines.append(f'⏳ <b>До финиша:</b> {_fmt_remaining(end_normalized, now_utc)}')
    info_lines.append(f'🎯 <b>+1 зачёт</b> {_rule_short(contest.contest_type)}')
    info_block = '<blockquote>' + '\n'.join(info_lines) + '</blockquote>'

    how_to_block = '<blockquote>' + _how_to_play(contest.contest_type) + '</blockquote>'

    progress_lines: list[str] = []
    if user_rank:
        progress_lines.append(f'🏅 Место: <b>{user_rank}</b> | Зачётов: <b>{user_score}</b>')
        progress_lines.extend(_progress_status_lines(user_rank, gap_to_prize))
    else:
        progress_lines.append('Вы ещё не участвуете.')
        progress_lines.append('Пригласите первого друга — и вы в таблице!')
    progress_block = '<b>👤 Ваш прогресс</b>\n<blockquote>' + '\n'.join(progress_lines) + '</blockquote>'

    if leaderboard:
        lb_items: list[str] = []
        for idx, (name, score, _amount, is_virtual) in enumerate(leaderboard[:10], start=1):
            prefix = _rank_prefix(idx)
            suffix = ' 👻' if is_virtual else ''
            lb_items.append(f'{prefix} {html.escape(name)}{suffix} — <b>{score}</b>')
        lb_body = '\n'.join(lb_items)
        lb_block = f'<b>🏅 Топ участников</b>\n<blockquote expandable>{lb_body}</blockquote>'
    else:
        lb_block = '<b>🏅 Топ участников</b>\n<blockquote>Пока пусто — будьте первым!</blockquote>'

    return '\n\n'.join([header, info_block, how_to_block, progress_block, lb_block])


PRIZE_PLACES = 3


def _plural_days(n: int) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return 'день'
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return 'дня'
    return 'дней'


def _plural_hours(n: int) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return 'час'
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return 'часа'
    return 'часов'


def _fmt_remaining_short(end_utc: datetime, now_utc: datetime) -> str:
    total_seconds = int((end_utc - now_utc).total_seconds())
    if total_seconds <= 0:
        return 'скоро финиш'
    days = total_seconds // 86400
    if days >= 1:
        return f'{days} {_plural_days(days)}'
    hours = max(total_seconds // 3600, 1)
    return f'{hours} {_plural_hours(hours)}'


async def _compute_gap_to_prize(
    db: AsyncSession,
    contest_id: int,
    user_rank: int | None,
    user_score: int,
) -> int | None:
    """Referrals needed to enter the prize zone (last prize slot).

    Returns None when user is already in prize zone (top-PRIZE_PLACES).
    """
    if user_rank is not None and user_rank <= PRIZE_PLACES:
        return None

    target_rank = PRIZE_PLACES

    real_lb = await get_contest_leaderboard(db, contest_id)
    virtual = await list_virtual_participants(db, contest_id)
    merged: list[tuple[int, int]] = [(score, amount) for _user, score, amount in real_lb]
    merged.extend((vp.referral_count, vp.total_amount_kopeks) for vp in virtual)
    merged.sort(key=lambda x: (-x[0], -x[1]))

    if len(merged) >= target_rank:
        target_score = merged[target_rank - 1][0]
        return max(target_score - user_score + 1, 1)
    return 1


def _progress_status_lines(user_rank: int | None, gap: int | None) -> list[str]:
    """Status lines shown after the rank/score line, depending on user's position."""
    if user_rank == 1:
        return ['🥇 Вы лидируете! Держите первое место']
    if user_rank == 2:
        return ['🥈 Вы в призовой зоне — удержите место']
    if user_rank == 3:
        return ['🥉 Вы в призовой зоне — удержите место']
    if gap and gap > 0:
        return [f'➡️ До призового места: <b>{gap}</b>']
    return []


async def build_main_menu_contest_block(db: AsyncSession, user_id: int | None) -> str:
    """Compact contest footer for /start main menu. Empty if no active contest."""
    contest = await _get_current_contest(db)
    if not contest:
        return ''

    end_normalized = _normalize_end(contest.end_at)
    now_utc = datetime.now(UTC)

    lines: list[str] = [f'🏆 <b>{html.escape(contest.title)}</b>']
    if contest.prize_text:
        lines.append(f'🎁 {html.escape(contest.prize_text)} <i>(топ-3)</i>')
    lines.append(f'⏳ До финиша: <b>{_fmt_remaining_short(end_normalized, now_utc)}</b>')

    cta_line = '💡 Откройте «🏆 Конкурсы» и пригласите друзей'

    if user_id is not None:
        user_score = await get_referrer_score(db, contest.id, user_id)
        user_rank = await _compute_user_rank(db, contest.id, user_id) if user_score > 0 else None
        gap = await _compute_gap_to_prize(db, contest.id, user_rank, user_score)

        if user_rank:
            lines.append(f'👤 Ваше место: <b>{user_rank}</b> | <b>{user_score}</b> зачётов')
            lines.extend(_progress_status_lines(user_rank, gap))
            if user_rank > PRIZE_PLACES and gap and gap > 0:
                lines.append(cta_line)
        else:
            lines.append('👤 Вы ещё не участвуете')
            lines.append(cta_line)
    else:
        lines.append(cta_line)

    return '<blockquote>' + '\n'.join(lines) + '</blockquote>'


@auth_required
@error_handler
async def show_contests_menu(callback: types.CallbackQuery, db_user, db: AsyncSession):
    """Show the lowest-id active referral contest with leaderboard."""
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if 'query is too old' not in msg and 'query id is invalid' not in msg:
            raise

    texts = get_texts(db_user.language)

    contest = await _get_current_contest(db)

    if not contest:
        text = texts.t(
            'CONTEST_NO_ACTIVE',
            '🏆 <b>Реферальные конкурсы</b>\n<blockquote>Сейчас нет активных конкурсов.\nЗагляните позже!</blockquote>',
        )
        keyboard = _build_keyboard(has_contest=False, language=db_user.language)
    else:
        leaderboard = await get_contest_leaderboard_with_virtual(db, contest.id, limit=10)
        user_score = await get_referrer_score(db, contest.id, db_user.id)
        user_rank = await _compute_user_rank(db, contest.id, db_user.id) if user_score > 0 else None
        gap_to_prize = await _compute_gap_to_prize(db, contest.id, user_rank, user_score)

        text = _build_contest_text(
            contest=contest,
            leaderboard=leaderboard,
            user_rank=user_rank,
            user_score=user_score,
            gap_to_prize=gap_to_prize,
            now_utc=datetime.now(UTC),
        )
        keyboard = _build_keyboard(has_contest=True, language=db_user.language)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if 'message is not modified' in msg:
            return
        if 'message to edit not found' in msg or 'message can\'t be edited' in msg:
            await callback.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
            return
        raise


def register_handlers(dp: Dispatcher):
    """Register contests handler."""
    dp.callback_query.register(show_contests_menu, F.data == 'contests_menu')
