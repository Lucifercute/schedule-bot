from __future__ import annotations

import re
from dataclasses import dataclass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DAYS = ["ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ"]


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return (r / 255, g / 255, b / 255)


# Постоянные цвета заливки ячеек в таблице расписания → статус занятия.
# Коды фиксированы на стороне таблицы и не меняются.
STATUS_COLORS: dict[str, str] = {
    "#e6b8af": "Занятие отменено",
    "#d9ead3": "Занятие проведено",
    "#ffff00": "Занятие назначено",
    "#fff2cc": "Занятие назначено и подтверждено",
    "#00ffff": "Назначено на этот же день/время на следующей неделе",
}
_STATUS_BY_RGB = {_hex_to_rgb(hex_code): label for hex_code, label in STATUS_COLORS.items()}

# Допуск на погрешность округления цвета при чтении из Google Sheets API.
_COLOR_MATCH_THRESHOLD = 0.001


@dataclass
class Training:
    day: str
    date: str
    time: str
    status: str
    text: str


@dataclass
class WeekSchedule:
    trainings: list[Training]
    salary_total: str | None


def _get_service(service_account_file: str):
    creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def find_current_week_sheet(service, spreadsheet_id: str) -> tuple[str, int]:
    """Возвращает (название листа, число строк на нём) для листа с пометкой 'текущая'."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta["sheets"]:
        props = sheet["properties"]
        if "текущая" in props["title"].lower():
            return props["title"], props["gridProperties"]["rowCount"]
    raise ValueError("Не нашёл лист с пометкой 'текущая' в названии")


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _find_day_row_index(grid: list[dict]) -> int:
    """Ищет строку с названиями дней недели (ПОНЕДЕЛЬНИК, ВТОРНИК, ...) —
    не полагаемся на фиксированный номер строки, т.к. таблица может содержать
    служебные строки-уведомления (отпуск и т.п.) над или под ней."""
    day_set = set(DAYS)
    for idx, row in enumerate(grid):
        values = row.get("values")
        if not values:
            continue
        texts = {v.get("formattedValue", "").strip().upper() for v in values[1:]}
        if day_set & texts:
            return idx
    raise ValueError("Не нашёл строку с названиями дней недели")


def _find_time_start_row(grid: list[dict], start_from: int) -> int:
    """Ищет первую строку с временем в колонке A (вида '10:00'), начиная с start_from.
    Между строкой дней и первой строкой времени может быть произвольное число
    служебных строк-уведомлений — пропускаем их все."""
    for idx in range(start_from, len(grid)):
        values = grid[idx].get("values")
        if not values:
            continue
        first_cell = values[0].get("formattedValue", "").strip()
        if _TIME_RE.match(first_cell):
            return idx
    raise ValueError("Не нашёл начало сетки времени (ожидал ячейку вида '10:00' в колонке A)")


def _find_value_after_label(grid: list[dict], label: str) -> str | None:
    """Ищет ячейку, текст которой начинается с label (например 'Итого в ЗП:'),
    и возвращает текст соседней ячейки справа."""
    for row in grid:
        values = row.get("values")
        if not values:
            continue
        for idx, cell in enumerate(values):
            text = cell.get("formattedValue", "").strip()
            if text.startswith(label):
                if idx + 1 < len(values):
                    return values[idx + 1].get("formattedValue")
                return None
    return None


def _rgb(color: dict) -> tuple[float, float, float]:
    return (color.get("red", 0.0), color.get("green", 0.0), color.get("blue", 0.0))


def _color_distance(c1: tuple, c2: tuple) -> float:
    return sum((a - b) ** 2 for a, b in zip(c1, c2))


def _match_status(bg: dict) -> str | None:
    """Сопоставляет цвет фона ячейки с известным статусом.
    Серые служебные ячейки (ФРИРАЙД/СОБРАНИЕ и т.п.) и пустые — не совпадут ни с чем, вернём None."""
    if not bg:
        return None
    color = _rgb(bg)
    for known_color, label in _STATUS_BY_RGB.items():
        if _color_distance(color, known_color) <= _COLOR_MATCH_THRESHOLD:
            return label
    return None


def get_week_schedule(service_account_file: str, spreadsheet_id: str) -> WeekSchedule:
    service = _get_service(service_account_file)
    sheet_title, row_count = find_current_week_sheet(service, spreadsheet_id)

    result = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[f"'{sheet_title}'!A1:H{row_count}"],
        includeGridData=True,
    ).execute()

    grid = result["sheets"][0]["data"][0]["rowData"]

    day_row_idx = _find_day_row_index(grid)
    time_start_idx = _find_time_start_row(grid, day_row_idx + 1)

    date_row = grid[day_row_idx - 1]["values"]
    day_row = grid[day_row_idx]["values"]

    trainings: list[Training] = []
    for row_idx in range(time_start_idx, len(grid)):
        row = grid[row_idx]
        if "values" not in row or not row["values"]:
            continue
        time_cell = row["values"][0].get("formattedValue")
        if not time_cell:
            continue
        for col_idx in range(1, min(8, len(row["values"]))):
            cell = row["values"][col_idx]
            text = cell.get("formattedValue")
            if not text:
                continue
            bg = cell.get("userEnteredFormat", {}).get("backgroundColor", {})
            status = _match_status(bg)
            if status is None:
                continue
            day_name = day_row[col_idx].get("formattedValue", "") if col_idx < len(day_row) else ""
            date = date_row[col_idx].get("formattedValue", "") if col_idx < len(date_row) else ""
            trainings.append(Training(day=day_name, date=date, time=time_cell, status=status, text=text))

    salary_total = _find_value_after_label(grid, "Итого в ЗП")
    return WeekSchedule(trainings=trainings, salary_total=salary_total)
