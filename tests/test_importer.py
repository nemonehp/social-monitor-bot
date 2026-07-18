from pathlib import Path

from app.services.importer import parse_delimited


def test_user_table_columns(tmp_path: Path):
    path = tmp_path / "sources.csv"
    path.write_text(
        "Федеральный округ;Регион;Ссылка на ТГ-канал РО;Название TG;Статус TG;Ссылка на группу ВК РО;Название ВК;Статус ВК\n"
        "ДФО;Амурская область;https://t.me/example_name;TG title;Рабочий;https://vk.com/club123;VK title;Рабочая\n",
        encoding="utf-8",
    )
    preview = parse_delimited(path)
    assert len(preview.candidates) == 2
    assert {x.platform.value for x in preview.candidates} == {"vk", "telegram"}
    assert all(x.federal_district == "ДФО" for x in preview.candidates)
    assert all(x.category == "ДФО" for x in preview.candidates)
    assert all(x.subcategory == "Амурская область" for x in preview.candidates)
