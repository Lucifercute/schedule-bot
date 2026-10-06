import asyncio
import datetime
import json
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from config import (
    TELEGRAM_BOT_TOKEN,
    SPREADSHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    ALLOWED_TELEGRAM_USER_ID,
)
from sheets_client import get_week_schedule, Training, WeekSchedule, DAYS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SHEETS_TIMEOUT_SECONDS = 30

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
POLL_INTERVAL_SECONDS = 600  # 10 минут
CONFIRMED_STATUS = "Занятие назначено и подтверждено"


def _has_access(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ALLOWED_TELEGRAM_USER_ID


def _format_training(t: Training) -> str:
    first_line = t.text.splitlines()[0] if t.text else ""
    return f"  {t.time} [{t.status}]\n    {first_line}"


def _training_key(t: Training) -> str:
    return f"{t.date}|{t.time}"


def _week_id(trainings: list[Training]) -> str:
    return ",".join(sorted({t.date for t in trainings}))


def _snapshot(trainings: list[Training]) -> dict[str, str]:
    return {_training_key(t): t.status for t in trainings}


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_state(week_id: str, trainings: list[Training]) -> None:
    state = {"week_id": week_id, "snapshot": _snapshot(trainings)}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _format_notification(header: str, t: Training) -> str:
    first_line = t.text.splitlines()[0] if t.text else ""
    return f"{header}\n{t.day.capitalize()}, {t.date} {t.time}\n{first_line}\nСтатус: {t.status}"


async def _fetch_schedule() -> WeekSchedule:
    """get_week_schedule ходит в Google API синхронно (блокирующе).
    Выполняем в отдельном потоке с таймаутом, чтобы зависший запрос
    не остановил весь event loop (и polling Telegram, и фоновую проверку)."""
    return await asyncio.wait_for(
        asyncio.to_thread(get_week_schedule, GOOGLE_SERVICE_ACCOUNT_FILE, SPREADSHEET_ID),
        timeout=SHEETS_TIMEOUT_SECONDS,
    )


async def _check_for_changes() -> None:
    trainings = (await _fetch_schedule()).trainings
    week_id = _week_id(trainings)
    state = _load_state()

    if state.get("week_id") != week_id:
        # Первый запуск или сменилась неделя ("текущая" переехала на другой лист) —
        # запоминаем как есть, без уведомлений задним числом по всей новой неделе.
        _save_state(week_id, trainings)
        return

    old_snapshot: dict[str, str] = state.get("snapshot", {})
    for t in trainings:
        key = _training_key(t)
        old_status = old_snapshot.get(key)
        if old_status is None:
            await bot.send_message(ALLOWED_TELEGRAM_USER_ID, _format_notification("🆕 Новая тренировка", t))
        elif old_status != t.status and t.status == CONFIRMED_STATUS:
            await bot.send_message(ALLOWED_TELEGRAM_USER_ID, _format_notification("✅ Тренировка подтверждена", t))

    _save_state(week_id, trainings)


async def _poll_loop() -> None:
    while True:
        try:
            await _check_for_changes()
        except Exception:
            logging.exception("Ошибка при проверке изменений расписания")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not _has_access(message):
        return
    try:
        trainings = (await _fetch_schedule()).trainings
    except Exception:
        logging.exception("Не удалось получить расписание для /today")
        await message.answer("Не получилось прочитать таблицу, попробуй ещё раз чуть позже.")
        return
    today_name = DAYS[datetime.date.today().weekday()]
    today_trainings = sorted(
        (t for t in trainings if t.day == today_name), key=lambda t: t.time
    )
    if not today_trainings:
        await message.answer(f"На сегодня ({today_name.lower()}) тренировок не найдено.")
        return
    lines = [f"Тренировки на сегодня ({today_name.lower()}):", ""]
    lines += [_format_training(t) for t in today_trainings]
    await message.answer("\n".join(lines))


@dp.message(Command("week"))
async def cmd_week(message: Message) -> None:
    if not _has_access(message):
        return
    try:
        schedule = await _fetch_schedule()
    except Exception:
        logging.exception("Не удалось получить расписание для /week")
        await message.answer("Не получилось прочитать таблицу, попробуй ещё раз чуть позже.")
        return
    trainings = schedule.trainings
    if not trainings:
        await message.answer("На эту неделю тренировок не найдено.")
        return

    by_day: dict[str, list[Training]] = {}
    for t in trainings:
        by_day.setdefault(t.day, []).append(t)

    lines = ["Расписание на неделю:", ""]
    for day in DAYS:
        if day not in by_day:
            continue
        day_trainings = sorted(by_day[day], key=lambda t: t.time)
        lines.append(f"{day.capitalize()} ({day_trainings[0].date}):")
        lines += [_format_training(t) for t in day_trainings]
        lines.append("")
    if schedule.salary_total:
        lines.append(f"Итого в ЗП: {schedule.salary_total}")
    await message.answer("\n".join(lines))


async def main() -> None:
    asyncio.create_task(_poll_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
