# tests/test_characters.py
"""Карточки персонажей (блок 2 релиза 1.6.0).

Что проверяем:

* characters.json хранит карточку ``{gender, pronouns, address, manner, description}``,
  а старый формат (строка) мигрирует без потери данных и с бэкапом файла;
* ответ модели с карточкой разбирается и проверяется (заглушки, CJK-мусор);
* карточка говорящего подставляется в промпт перевода;
* для неизвестного персонажа подставляются значения по умолчанию.

Все тесты работают без сети и в своей временной папке data/.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brain import llm_engine as llm  # noqa: E402
from brain import memory_engine  # noqa: E402
from brain import paths as paths_mod  # noqa: E402
from brain.memory_engine import (  # noqa: E402
    DEFAULT_CHARACTER_CARD,
    format_character_line,
    format_characters_for_prompt,
    get_character_card,
    load_characters,
    migrate_characters_file,
    normalize_character_card,
    save_character,
)

ALICE_CARD = {
    "gender": "female",
    "pronouns": "she/her",
    "address": "ty",
    "manner": "мягкая, меланхоличная",
    "description": "Хранительница маяка",
}
BOB_CARD = {
    "gender": "male",
    "pronouns": "he/him",
    "address": "vy",
    "manner": "сдержанный, формальный",
    "description": "Старший смотритель",
}


class TempDataDirCase(unittest.TestCase):
    """Каждый тест работает в своей временной папке data/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gadget_chars_")
        self._old_env = os.environ.get("GADGET_DATA_DIR")
        os.environ["GADGET_DATA_DIR"] = self.tmp
        paths_mod.resolve_data_dir(force=True)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("GADGET_DATA_DIR", None)
        else:
            os.environ["GADGET_DATA_DIR"] = self._old_env
        paths_mod.resolve_data_dir(force=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_characters(self, data):
        path = memory_engine.characters_path()
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        return path

    def read_characters(self):
        with open(memory_engine.characters_path(), encoding="utf-8") as stream:
            return json.load(stream)


# --------------------------------------------------------------------------- #
#  Нормализация карточки
# --------------------------------------------------------------------------- #
class CardNormalizationTests(unittest.TestCase):
    def test_legacy_string_becomes_description(self):
        card = normalize_character_card("Хранительница маяка")
        self.assertEqual(card["description"], "Хранительница маяка")
        self.assertEqual(card["gender"], "unknown")
        self.assertEqual(card["pronouns"], "they/them")
        self.assertEqual(card["address"], "ty")
        self.assertEqual(card["manner"], "")

    def test_full_card_is_kept(self):
        self.assertEqual(normalize_character_card(dict(ALICE_CARD)), ALICE_CARD)

    def test_missing_fields_get_defaults(self):
        card = normalize_character_card({"description": "Старший смотритель"})
        self.assertEqual(card["description"], "Старший смотритель")
        self.assertEqual(card["gender"], "unknown")
        self.assertEqual(card["address"], "ty")

    def test_pronouns_derived_from_gender(self):
        self.assertEqual(
            normalize_character_card({"gender": "female"})["pronouns"], "she/her"
        )
        self.assertEqual(
            normalize_character_card({"gender": "male"})["pronouns"], "he/him"
        )

    def test_gender_derived_from_pronouns_when_unknown(self):
        # qwen2.5:3b обычно отдаёт gender="unknown", но верные местоимения.
        male = normalize_character_card({"pronouns": "he/him"})
        self.assertEqual(male["gender"], "male")
        self.assertEqual(male["pronouns"], "he/him")

        female = normalize_character_card({"pronouns": "she/her"})
        self.assertEqual(female["gender"], "female")
        self.assertEqual(female["pronouns"], "she/her")

    def test_neutral_pronouns_keep_gender_unknown(self):
        card = normalize_character_card({"pronouns": "they/them"})
        self.assertEqual(card["gender"], "unknown")
        self.assertEqual(card["pronouns"], "they/them")

    def test_explicit_gender_wins_over_pronouns(self):
        # Карточка не должна говорить «female» и «he/him» одновременно.
        card = normalize_character_card({"gender": "female", "pronouns": "he/him"})
        self.assertEqual(card["gender"], "female")
        self.assertEqual(card["pronouns"], "she/her")

    def test_unknown_gender_and_address_fall_back(self):
        card = normalize_character_card(
            {"gender": "робот", "address": "господин", "description": "Страж"}
        )
        self.assertEqual(card["gender"], "unknown")
        self.assertEqual(card["address"], "ty")

    def test_address_is_case_insensitive(self):
        self.assertEqual(normalize_character_card({"address": "VY"})["address"], "vy")

    def test_description_argument_wins(self):
        card = normalize_character_card(dict(ALICE_CARD), description="Другое")
        self.assertEqual(card["description"], "Другое")
        self.assertEqual(card["gender"], "female")

    def test_extra_fields_preserved(self):
        card = normalize_character_card({"description": "Страж", "voice": "тихий"})
        self.assertEqual(card["voice"], "тихий")

    def test_none_is_default_card(self):
        self.assertEqual(normalize_character_card(None), DEFAULT_CHARACTER_CARD)

    def test_default_card_is_not_shared(self):
        first = normalize_character_card(None)
        first["gender"] = "female"
        self.assertEqual(normalize_character_card(None)["gender"], "unknown")


# --------------------------------------------------------------------------- #
#  Миграция characters.json
# --------------------------------------------------------------------------- #
class CharactersMigrationTests(TempDataDirCase):
    def test_legacy_file_is_read_as_cards(self):
        self.write_characters({"Alice": "Хранительница маяка", "Я": "Рассказчик"})
        cards = load_characters()
        self.assertEqual(cards["Alice"]["description"], "Хранительница маяка")
        self.assertEqual(cards["Alice"]["gender"], "unknown")
        self.assertEqual(cards["Я"]["address"], "ty")

    def test_reading_does_not_rewrite_file(self):
        # Чтение не должно молча менять данные пользователя: миграция отдельная.
        self.write_characters({"Alice": "Хранительница маяка"})
        load_characters()
        self.assertEqual(self.read_characters(), {"Alice": "Хранительница маяка"})

    def test_migrate_rewrites_and_keeps_data(self):
        self.write_characters({"Alice": "Хранительница маяка", "Bob": "Смотритель"})
        migrated, backup = migrate_characters_file()

        self.assertEqual(migrated, 2)
        self.assertTrue(backup and os.path.exists(backup))

        on_disk = self.read_characters()
        self.assertEqual(on_disk["Alice"]["description"], "Хранительница маяка")
        self.assertEqual(on_disk["Bob"]["description"], "Смотритель")
        self.assertEqual(on_disk["Alice"]["gender"], "unknown")

    def test_migrate_creates_backup_with_old_format(self):
        self.write_characters({"Alice": "Хранительница маяка"})
        _, backup = migrate_characters_file()
        with open(backup, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"Alice": "Хранительница маяка"})

    def test_migrate_is_idempotent(self):
        self.write_characters({"Alice": "Хранительница маяка"})
        self.assertEqual(migrate_characters_file()[0], 1)
        self.assertEqual(migrate_characters_file(), (0, None))

    def test_migrate_without_file_is_noop(self):
        self.assertEqual(migrate_characters_file(), (0, None))

    def test_migrate_keeps_new_format_untouched(self):
        self.write_characters({"Alice": ALICE_CARD})
        self.assertEqual(migrate_characters_file(), (0, None))
        self.assertEqual(self.read_characters()["Alice"], ALICE_CARD)

    def test_new_card_is_saved_with_backup_of_legacy_file(self):
        self.write_characters({"Alice": "Хранительница маяка"})
        self.assertTrue(save_character("Bob", card=BOB_CARD))

        on_disk = self.read_characters()
        self.assertEqual(on_disk["Bob"], BOB_CARD)
        # Старая запись тоже переехала в карточку и не потерялась.
        self.assertEqual(on_disk["Alice"]["description"], "Хранительница маяка")
        backups = [name for name in os.listdir(self.tmp)
                   if name.startswith("characters.json.bak-")]
        self.assertEqual(len(backups), 1)

    def test_save_character_does_not_overwrite(self):
        self.assertTrue(save_character("Alice", card=ALICE_CARD))
        self.assertFalse(save_character("Alice", card=BOB_CARD))
        self.assertEqual(load_characters()["Alice"]["gender"], "female")

    def test_save_character_requires_description(self):
        self.assertFalse(save_character("Alice"))
        self.assertFalse(save_character("Alice", description=""))
        self.assertEqual(load_characters(), {})

    def test_save_character_keeps_card_fields(self):
        self.assertTrue(save_character("Bob", card=BOB_CARD))
        card = load_characters()["Bob"]
        self.assertEqual(card["gender"], "male")
        self.assertEqual(card["address"], "vy")
        self.assertEqual(card["manner"], "сдержанный, формальный")


# --------------------------------------------------------------------------- #
#  Поиск карточки
# --------------------------------------------------------------------------- #
class CharacterCardLookupTests(TempDataDirCase):
    def test_known_character(self):
        save_character("Alice", card=ALICE_CARD)
        self.assertEqual(get_character_card("Alice"), ALICE_CARD)

    def test_unknown_character_gets_defaults(self):
        save_character("Alice", card=ALICE_CARD)
        self.assertEqual(get_character_card("Zoe"), DEFAULT_CHARACTER_CARD)

    def test_lookup_is_case_insensitive(self):
        save_character("Alice", card=ALICE_CARD)
        self.assertEqual(get_character_card("alice")["gender"], "female")

    def test_empty_name_gets_defaults(self):
        self.assertEqual(get_character_card(""), DEFAULT_CHARACTER_CARD)

    def test_returned_card_is_a_copy(self):
        save_character("Alice", card=ALICE_CARD)
        card = get_character_card("Alice")
        card["gender"] = "male"
        self.assertEqual(get_character_card("Alice")["gender"], "female")


# --------------------------------------------------------------------------- #
#  Форматирование для промпта
# --------------------------------------------------------------------------- #
class CharactersPromptFormatTests(unittest.TestCase):
    def test_line_contains_card_fields(self):
        line = format_character_line("Alice", ALICE_CARD)
        self.assertIn("Alice", line)
        self.assertIn("gender=female", line)
        self.assertIn("pronouns=she/her", line)
        self.assertIn("address=ty (ты)", line)
        self.assertIn("мягкая, меланхоличная", line)
        self.assertIn("Хранительница маяка", line)

    def test_line_without_manner(self):
        line = format_character_line("Bob", {"description": "Смотритель"})
        self.assertNotIn("manner=", line)
        self.assertIn("address=ty (ты)", line)

    def test_vy_is_rendered_as_formal(self):
        self.assertIn("address=vy (вы)", format_character_line("Bob", BOB_CARD))

    def test_prompt_lists_every_character(self):
        prompt = format_characters_for_prompt(
            {"Alice": ALICE_CARD, "Bob": BOB_CARD}
        )
        self.assertIn("Alice", prompt)
        self.assertIn("Bob", prompt)
        self.assertIn("gender=male", prompt)

    def test_empty_characters_give_empty_prompt(self):
        self.assertEqual(format_characters_for_prompt({}), "")


# --------------------------------------------------------------------------- #
#  Промпт анализа лора и разбор ответа
# --------------------------------------------------------------------------- #
class LorePromptTests(unittest.TestCase):
    def test_prompt_asks_for_full_card(self):
        prompt = llm._build_lore_prompt("Alice", "Alice: hello")
        for field in ("gender", "pronouns", "address", "manner", "description"):
            self.assertIn(f'"{field}"', prompt)
        self.assertIn('"Alice"', prompt)
        self.assertIn("Alice: hello", prompt)
        self.assertIn('Answer {} ONLY', prompt)


class ParseCharacterCardsTests(unittest.TestCase):
    def test_full_card(self):
        raw = json.dumps({"Alice": ALICE_CARD}, ensure_ascii=False)
        self.assertEqual(llm._parse_character_cards(raw), {"Alice": ALICE_CARD})

    def test_fenced_json(self):
        raw = "```json\n" + json.dumps({"Bob": BOB_CARD}, ensure_ascii=False) + "\n```"
        parsed = llm._parse_character_cards(raw)
        self.assertEqual(parsed["Bob"]["address"], "vy")

    def test_legacy_string_value_becomes_description(self):
        parsed = llm._parse_character_cards('{"Alice": "Хранительница маяка"}')
        self.assertEqual(parsed["Alice"]["description"], "Хранительница маяка")
        self.assertEqual(parsed["Alice"]["gender"], "unknown")
        self.assertEqual(parsed["Alice"]["address"], "ty")

    def test_missing_fields_are_normalized(self):
        parsed = llm._parse_character_cards(
            '{"Alice": {"gender": "female", "description": "Хранительница маяка"}}'
        )
        self.assertEqual(parsed["Alice"]["pronouns"], "she/her")
        self.assertEqual(parsed["Alice"]["address"], "ty")

    def test_qwen_style_card_gets_gender_from_pronouns(self):
        # Реальный ответ qwen2.5:3b: пол unknown, но местоимения he/him.
        raw = json.dumps(
            {"Bob": {"gender": "unknown", "pronouns": "he/him", "address": "ty",
                     "manner": "сдержанный", "description": "Старший смотритель"}},
            ensure_ascii=False,
        )
        parsed = llm._parse_character_cards(raw)
        self.assertEqual(parsed["Bob"]["gender"], "male")
        self.assertEqual(parsed["Bob"]["pronouns"], "he/him")

    def test_empty_object_means_no_character(self):
        self.assertEqual(llm._parse_character_cards("{}"), {})

    def test_placeholder_description_rejected(self):
        raw = '{"Alice": {"description": "описание на русском"}}'
        self.assertEqual(llm._parse_character_cards(raw), {})

    def test_short_description_rejected(self):
        raw = '{"Alice": {"description": "Он"}}'
        self.assertEqual(llm._parse_character_cards(raw), {})

    def test_cjk_description_rejected(self):
        raw = '{"Alice": {"description": "Это вы听起来像是一个角色。"}}'
        self.assertEqual(llm._parse_character_cards(raw), {})

    def test_cjk_manner_is_cleared_but_card_kept(self):
        raw = json.dumps(
            {"Alice": dict(ALICE_CARD, manner="听起来像是一个角色")},
            ensure_ascii=False,
        )
        parsed = llm._parse_character_cards(raw)
        self.assertEqual(parsed["Alice"]["description"], "Хранительница маяка")
        self.assertEqual(parsed["Alice"]["manner"], "")

    def test_garbage_returns_empty(self):
        self.assertEqual(llm._parse_character_cards("не JSON"), {})
        self.assertEqual(llm._parse_character_cards(None), {})
        self.assertEqual(llm._parse_character_cards('["Alice"]'), {})

    def test_parse_lore_json_still_returns_descriptions(self):
        # Старый контракт 1.5.2 остаётся рабочим.
        raw = json.dumps({"Alice": ALICE_CARD}, ensure_ascii=False)
        self.assertEqual(
            llm._parse_lore_json(raw), {"Alice": "Хранительница маяка"}
        )


# --------------------------------------------------------------------------- #
#  Определение говорящего
# --------------------------------------------------------------------------- #
class SpeakerDetectionTests(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(llm.speaker_from_text("Alice: hello"), "Alice")

    def test_two_word_name(self):
        self.assertEqual(llm.speaker_from_text("John Smith: hi"), "John Smith")

    def test_me_is_internal_voice(self):
        self.assertEqual(llm.speaker_from_text("Me: I should go"), "Я")
        # «Me, quietly» — не тот шаблон, что даёт OCR визуальной новеллы:
        # внутренний голос распознаётся по «Me:», «Me » и «I ».
        self.assertIsNone(llm.speaker_from_text("me, quietly"))

    def test_first_person_is_internal_voice(self):
        self.assertEqual(llm.speaker_from_text("I should go back"), "Я")

    def test_garbage_names_are_rejected(self):
        self.assertIsNone(llm.speaker_from_text("Surprisingly: yes"))
        self.assertIsNone(llm.speaker_from_text("Suddenly: a noise"))

    def test_no_colon(self):
        self.assertIsNone(llm.speaker_from_text("just a plain line"))

    def test_empty(self):
        self.assertIsNone(llm.speaker_from_text(""))
        self.assertIsNone(llm.speaker_from_text(None))


# --------------------------------------------------------------------------- #
#  Карточка в промпте перевода
# --------------------------------------------------------------------------- #
class TranslationPromptCardTests(TempDataDirCase):
    def _prompt(self, text):
        with mock.patch.object(llm, "_get_web_context", return_value=""):
            return llm._build_translation_prompt(text)

    def test_known_speaker_card_is_injected(self):
        save_character("Alice", card=ALICE_CARD)
        _, user = self._prompt('Alice: "The lighthouse is cold."')

        self.assertIn("SPEAKER CARD", user)
        self.assertIn("Говорящий: Alice", user)
        self.assertIn("Пол: female", user)
        self.assertIn("Обращение к собеседнику: ты (ty)", user)
        self.assertIn("мягкая, меланхоличная", user)
        self.assertIn("Хранительница маяка", user)

    def test_formal_address_is_injected(self):
        save_character("Bob", card=BOB_CARD)
        _, user = self._prompt('Bob: "Report, please."')
        self.assertIn("Обращение к собеседнику: вы (vy)", user)

    def test_unknown_speaker_gets_defaults(self):
        _, user = self._prompt('Zoe: "Nobody knows me."')
        self.assertIn("Говорящий: Zoe", user)
        self.assertIn("Пол: unknown", user)
        self.assertIn("Обращение к собеседнику: ты (ty)", user)
        self.assertIn("Манера речи: не указана", user)

    def test_line_without_speaker_gets_defaults(self):
        _, user = self._prompt("A line with no speaker.")
        self.assertIn("Говорящий: неизвестен", user)
        self.assertIn("Пол: unknown", user)

    def test_system_prompt_is_unchanged(self):
        system, _ = self._prompt("Alice: hi")
        self.assertIn("localization engine", system)
        self.assertIn("NO INVENTED NAMES", system)


# --------------------------------------------------------------------------- #
#  Сохранение карточки из ответа модели
# --------------------------------------------------------------------------- #
class BackgroundAnalyzeCardTests(TempDataDirCase):
    def test_card_from_model_is_saved(self):
        raw = json.dumps({"Alice": ALICE_CARD}, ensure_ascii=False)
        saved = {}

        def _save(name, description=None, card=None):
            saved[name] = {"description": description, "card": card}
            return True

        with mock.patch.object(
            llm.web_search, "fetch_character_lore", return_value=""
        ), mock.patch.object(
            llm, "_lore_completion", return_value=raw
        ), mock.patch.object(llm, "save_character", side_effect=_save):
            llm.background_analyze(
                "Alice", "Alice: hi", {"provider": "ollama", "model": "x"}
            )

        self.assertEqual(saved["Alice"]["card"]["gender"], "female")
        self.assertEqual(saved["Alice"]["description"], None)
        self.assertEqual(saved["Alice"]["card"]["description"], "Хранительница маяка")

    def test_legacy_string_answer_is_saved(self):
        saved = {}

        def _save(name, description=None, card=None):
            saved[name] = card or {"description": description}
            return True

        with mock.patch.object(
            llm.web_search, "fetch_character_lore", return_value=""
        ), mock.patch.object(
            llm, "_lore_completion", return_value='{"Alice": "Хранительница маяка"}'
        ), mock.patch.object(llm, "save_character", side_effect=_save):
            llm.background_analyze(
                "Alice", "Alice: hi", {"provider": "ollama", "model": "x"}
            )

        self.assertEqual(saved["Alice"]["description"], "Хранительница маяка")
        self.assertEqual(saved["Alice"]["gender"], "unknown")

    def test_qwen_style_card_is_saved_with_derived_gender(self):
        # Без этого карточки от qwen2.5:3b оставались бы с gender=unknown.
        raw = json.dumps(
            {"Bob": {"gender": "unknown", "pronouns": "he/him", "address": "ty",
                     "manner": "сухой", "description": "Старший смотритель"}},
            ensure_ascii=False,
        )
        with mock.patch.object(
            llm.web_search, "fetch_character_lore", return_value=""
        ), mock.patch.object(llm, "_lore_completion", return_value=raw):
            llm.background_analyze(
                "Bob", "Bob: hi", {"provider": "ollama", "model": "x"}
            )

        card = load_characters()["Bob"]
        self.assertEqual(card["gender"], "male")
        self.assertEqual(card["pronouns"], "he/him")
        self.assertEqual(card["description"], "Старший смотритель")

    def test_duckduckgo_lore_saved_as_description(self):
        saved = {}

        def _save(name, description=None, card=None):
            saved[name] = description
            return True

        with mock.patch.object(
            llm.web_search, "fetch_character_lore", return_value="С вики."
        ), mock.patch.object(llm, "save_character", side_effect=_save):
            llm.background_analyze(
                "Alice", "Alice: hi", {"provider": "ollama", "model": "x"}
            )

        self.assertEqual(saved, {"Alice": "С вики."})


if __name__ == "__main__":
    unittest.main()
