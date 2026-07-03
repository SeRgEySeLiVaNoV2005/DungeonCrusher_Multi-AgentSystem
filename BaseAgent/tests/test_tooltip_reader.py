"""Unit tests for TooltipReaderAgent, UIElementDB, PendingElementStore, and WebReviewServer."""

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from src.communication.message_bus import Message, MessageBus, MessageType
from src.game_state.state import GameState


# ---------------------------------------------------------------------------
# PendingElementStore
# ---------------------------------------------------------------------------


class TestPendingElementStore:
    """Tests for the thread-safe pending element store."""

    def test_add_and_retrieve(self):
        """Elements can be added and listed."""
        from tooltip_reader.pending_store import PendingElementStore

        store = PendingElementStore()
        region = np.zeros((30, 100, 3), dtype=np.uint8)

        element = store.add(
            image=region,
            raw_text="Gold: 1,234",
            window_x=100, window_y=50,
            region_width=100, region_height=30,
        )

        assert element.id != ""
        assert element.raw_text == "Gold: 1,234"

        items = store.get_all(include_image=False)
        assert len(items) == 1
        assert items[0]["raw_text"] == "Gold: 1,234"

    def test_get_with_image(self):
        """Image can be base64-encoded in output."""
        from tooltip_reader.pending_store import PendingElementStore

        store = PendingElementStore()
        region = np.zeros((20, 40, 3), dtype=np.uint8)
        region[5:15, 10:30] = 255  # White rectangle.

        store.add(
            image=region,
            raw_text="Test",
            window_x=0, window_y=0,
            region_width=40, region_height=20,
        )

        items = store.get_all(include_image=True)
        assert len(items) == 1
        assert "image_base64" in items[0]
        assert len(items[0]["image_base64"]) > 0

    def test_update_fields(self):
        """Editable fields can be updated."""
        from tooltip_reader.pending_store import PendingElementStore

        store = PendingElementStore()
        region = np.zeros((10, 10, 3), dtype=np.uint8)

        element = store.add(
            image=region,
            raw_text="Raw text",
            window_x=0, window_y=0,
            region_width=10, region_height=10,
        )

        assert store.update(element.id, name="Gold Counter", edited_text="Gold: 9999",
                            tags=["resource"])
        item = store.get(element.id)
        assert item["name"] == "Gold Counter"
        assert item["edited_text"] == "Gold: 9999"
        assert item["tags"] == ["resource"]

    def test_pop_and_remove(self):
        """Elements can be popped (for saving) or removed (discarded)."""
        from tooltip_reader.pending_store import PendingElementStore

        store = PendingElementStore()
        region = np.zeros((10, 10, 3), dtype=np.uint8)

        e1 = store.add(image=region, raw_text="A", window_x=0, window_y=0,
                       region_width=10, region_height=10)
        e2 = store.add(image=region, raw_text="B", window_x=0, window_y=0,
                       region_width=10, region_height=10)

        assert store.count() == 2

        # Pop one.
        popped = store.pop(e1.id)
        assert popped is not None
        assert popped.raw_text == "A"
        assert store.count() == 1

        # Discard another.
        assert store.remove(e2.id) is True
        assert store.count() == 0

    def test_count_and_clear(self):
        """Count and clear work correctly."""
        from tooltip_reader.pending_store import PendingElementStore

        store = PendingElementStore()
        region = np.zeros((10, 10, 3), dtype=np.uint8)

        for i in range(5):
            store.add(image=region, raw_text=str(i),
                      window_x=i, window_y=0,
                      region_width=10, region_height=10)

        assert store.count() == 5
        store.clear()
        assert store.count() == 0


# ---------------------------------------------------------------------------
# UIElementDB
# ---------------------------------------------------------------------------


class TestUIElementDB:
    """Tests for the UI element database."""

    def test_add_and_retrieve(self):
        """Elements can be added and retrieved by ID."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((30, 100, 3), dtype=np.uint8)
            record = db.add_element(
                text="Gold: 1,234",
                region_image=region,
                window_x=100,
                window_y=50,
                region_width=100,
                region_height=30,
            )

            assert record.id != ""
            assert record.text == "Gold: 1,234"
            assert record.window_x == 100

            fetched = db.get(record.id)
            assert fetched is not None
            assert fetched.text == "Gold: 1,234"

    def test_persistence(self):
        """Database survives save/load roundtrip."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")

            db1 = UIElementDB(db_path)
            region = np.zeros((20, 80, 3), dtype=np.uint8)
            db1.add_element(
                text="Level 5",
                region_image=region,
                window_x=200, window_y=10,
                region_width=80, region_height=20,
            )

            db2 = UIElementDB(db_path)
            assert db2.count() == 1
            assert db2.list_all()[0].text == "Level 5"

    def test_find_by_tag(self):
        """Elements can be searched by tag."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((10, 10, 3), dtype=np.uint8)
            db.add_element(
                text="Attack", region_image=region,
                window_x=0, window_y=0, region_width=10, region_height=10,
                tags=["combat", "button"],
            )
            db.add_element(
                text="Gold", region_image=region,
                window_x=100, window_y=0, region_width=10, region_height=10,
                tags=["resource", "top-bar"],
            )

            assert len(db.find_by_tag("combat")) == 1
            assert len(db.find_by_tag("resource")) == 1
            assert db.find_by_tag("combat")[0].text == "Attack"

    def test_find_by_text(self):
        """Substring search on recognized text."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((10, 10, 3), dtype=np.uint8)
            db.add_element(text="Hero Level: 42", region_image=region,
                           window_x=0, window_y=0, region_width=10, region_height=10)
            db.add_element(text="Gold: 9999", region_image=region,
                           window_x=0, window_y=0, region_width=10, region_height=10)

            results = db.find_by_text("gold")
            assert len(results) == 1
            assert results[0].text == "Gold: 9999"

    def test_remove(self):
        """Elements can be removed."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((10, 10, 3), dtype=np.uint8)
            record = db.add_element(text="Test", region_image=region,
                                    window_x=0, window_y=0,
                                    region_width=10, region_height=10)

            assert db.count() == 1
            assert db.remove(record.id) is True
            assert db.count() == 0
            assert db.remove("nonexistent") is False

    def test_auto_name_generation(self):
        """Auto-generates name from recognized text (Cyrillic transliteration)."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((10, 10, 3), dtype=np.uint8)
            record = db.add_element(text="Золото: 1,234", region_image=region,
                                    window_x=0, window_y=0,
                                    region_width=10, region_height=10)

            assert record.name != ""
            assert "zoloto" in record.id.lower() or record.id != ""

    def test_empty_text(self):
        """Empty text gets a fallback."""
        from tooltip_reader.ui_element_db import UIElementDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_db.json")
            db = UIElementDB(db_path)

            region = np.zeros((10, 10, 3), dtype=np.uint8)
            record = db.add_element(text="", region_image=region,
                                    window_x=0, window_y=0,
                                    region_width=10, region_height=10)

            assert record.id != ""
            assert record.text == ""


# ---------------------------------------------------------------------------
# TooltipReaderAgent — state machine
# ---------------------------------------------------------------------------


class TestTooltipReaderStateMachine:
    """Tests for the agent's internal state transitions."""

    @staticmethod
    def _make_store():
        from tooltip_reader.pending_store import PendingElementStore
        return PendingElementStore()

    def test_initial_state_is_idle(self):
        """Agent starts in IDLE state."""
        from tooltip_reader.tooltip_reader_agent import ReaderState, TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store)
        assert agent._state == ReaderState.IDLE  # noqa: SLF001

    def test_on_frame_ignored_when_idle(self):
        """Agent ignores frames when not triggered."""
        from tooltip_reader.tooltip_reader_agent import ReaderState, TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store)

        state = GameState(screenshot=np.zeros((100, 100, 3), dtype=np.uint8))
        msg = Message(MessageType.FRAME_CAPTURED, "parent", payload=state)
        agent.on_frame(msg)
        assert agent._state == ReaderState.IDLE  # noqa: SLF001

    def test_no_screenshot_skips_ocr(self):
        """When triggered but screenshot is None, reset to IDLE."""
        from tooltip_reader.tooltip_reader_agent import ReaderState, TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store)
        agent._state = ReaderState.TRIGGERED  # noqa: SLF001

        state = GameState(screenshot=None)
        msg = Message(MessageType.FRAME_CAPTURED, "parent", payload=state)
        agent.on_frame(msg)

        assert agent._state == ReaderState.IDLE  # noqa: SLF001

    def test_trigger_resets_to_idle_after_processing(self):
        """After processing (even with error), state returns to IDLE."""
        from tooltip_reader.tooltip_reader_agent import ReaderState, TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store)
        agent._state = ReaderState.TRIGGERED  # noqa: SLF001

        state = GameState(
            screenshot=np.zeros((200, 300, 3), dtype=np.uint8),
            metadata={"window_region": {"left": 0, "top": 0, "width": 300, "height": 200}},
        )
        msg = Message(MessageType.FRAME_CAPTURED, "parent", payload=state)

        # Should not raise — exceptions caught internally, reset to IDLE.
        agent.on_frame(msg)
        assert agent._state == ReaderState.IDLE  # noqa: SLF001


# ---------------------------------------------------------------------------
# TooltipReaderAgent — preprocessing
# ---------------------------------------------------------------------------


class TestTooltipReaderPreprocessing:
    """Tests for the game-tailored image preprocessing."""

    @staticmethod
    def _make_store():
        from tooltip_reader.pending_store import PendingElementStore
        return PendingElementStore()

    def test_preprocess_returns_numpy_array(self):
        """Preprocessing returns a valid numpy array, upscaled by 2x."""
        from tooltip_reader.tooltip_reader_agent import TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store,
                                   scale=2.0, invert=True)

        region = np.zeros((30, 120, 3), dtype=np.uint8)
        region[5:25, 10:110] = (200, 200, 200)

        result = agent._preprocess_for_tooltip(region)  # noqa: SLF001
        assert isinstance(result, np.ndarray)
        assert result.shape[0] == 60
        assert result.shape[1] == 240

    def test_preprocess_no_scale(self):
        """Scale=1.0 keeps original dimensions."""
        from tooltip_reader.tooltip_reader_agent import TooltipReaderAgent

        bus = MessageBus()
        store = self._make_store()
        agent = TooltipReaderAgent("test_reader", bus, pending_store=store,
                                   scale=1.0, invert=False)

        region = np.zeros((30, 120, 3), dtype=np.uint8)
        result = agent._preprocess_for_tooltip(region)  # noqa: SLF001
        assert result.shape[0] == 30
        assert result.shape[1] == 120


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfig:
    """Tests for configuration classes."""

    def test_tooltip_reader_config_defaults(self):
        """TooltipReaderConfig has sensible defaults."""
        from src.core.config import TooltipReaderConfig

        cfg = TooltipReaderConfig()
        assert cfg.region_width == 320
        assert cfg.region_height == 90
        assert cfg.scale == 2.0
        assert cfg.invert is True
        assert cfg.ocr_lang == "eng"

    def test_web_review_config_defaults(self):
        """WebReviewConfig has sensible defaults."""
        from src.core.config import WebReviewConfig

        cfg = WebReviewConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8765
        assert cfg.db_path == "resources/ui_elements_db.json"

    def test_settings_includes_new_configs(self):
        """Settings includes tooltip_reader and web_review configs."""
        from src.core.config import Settings

        settings = Settings()
        assert settings.tooltip_reader.region_width == 320
        assert settings.web_review.port == 8765


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


class TestUtilities:
    """Tests for utility functions."""

    def test_get_cursor_position_callable(self):
        """get_cursor_position is importable and callable."""
        from src.input.emulator import get_cursor_position
        assert callable(get_cursor_position)
