import csv
from io import StringIO

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError


FIELDS = ("login", "game", "platform", "account_name", "game_subgroup", "purchase_cost", "extra_expenses")
ESCAPED_FIELDS = "autorent_escaped"
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10_000


def encode_accounts(accounts: list[dict]) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow((*FIELDS, ESCAPED_FIELDS))
    for account in accounts:
        values = []
        escaped = []
        for key in FIELDS:
            value = account[key]
            if isinstance(value, str) and (
                value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
            ):
                value = "'" + value
                escaped.append(key)
            values.append(value)
        writer.writerow((*values, " ".join(escaped)))
    return output.getvalue().encode("utf-8-sig")


def decode_accounts(data: bytes, model: type[BaseModel]) -> tuple[list[BaseModel], list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(422, "Сохраните CSV в кодировке UTF-8") from error
    accounts = []
    errors = []
    first_line = text.partition("\n")[0]
    delimiter = ";" if ";" in first_line and "," not in first_line else ","
    try:
        reader = csv.DictReader(StringIO(text), delimiter=delimiter, strict=True)
        if not reader.fieldnames or not {"login", "game"}.issubset(reader.fieldnames):
            raise HTTPException(422, "Требуются столбцы login и game")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise HTTPException(422, "Названия столбцов должны быть уникальными")
        for number, row in enumerate(reader, start=2):
            if number - 1 > MAX_ROWS:
                raise HTTPException(413, "В CSV больше 10 000 записей")
            if None in row:
                errors.append(f"Строка {reader.line_num}: лишние столбцы")
                continue
            values = {key: row[key] for key in FIELDS if row.get(key)}
            for key in (row.get(ESCAPED_FIELDS) or "").split():
                if key in values:
                    values[key] = values[key].removeprefix("'")
            try:
                accounts.append(model.model_validate(values))
            except ValidationError as error:
                fields = ", ".join(str(item["loc"][0]) for item in error.errors())
                errors.append(f"Строка {reader.line_num}: проверьте поля {fields}")
    except csv.Error as error:
        raise HTTPException(422, "Не удалось прочитать CSV. Проверьте кавычки и разделители") from error
    return accounts, errors
