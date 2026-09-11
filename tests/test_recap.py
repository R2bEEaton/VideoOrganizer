from pathlib import Path

from videoorganizer import recap
from videoorganizer.config import RecapChapter, RecapConfig, RecapSection


SECTIONS = [
    RecapSection("travel", ["airplane", "airport"]),
    RecapSection("water", ["beach", "snorkeling"]),
]


def test_classify_matches_tags():
    assert recap.classify_section(["airplane", "wing"], "", SECTIONS) == "travel"


def test_classify_matches_description():
    assert recap.classify_section([], "A quiet beach at dawn.", SECTIONS) == "water"


def test_classify_falls_back_to_other():
    assert recap.classify_section(["dog"], "A dog.", SECTIONS) == "other"


def test_classify_is_first_match_wins():
    """Section order in config is the tie-breaker, so it is meaningful."""
    assert recap.classify_section(["beach", "airport"], "", SECTIONS) == "travel"


def _config():
    return RecapConfig(
        default_chapter="main",
        sections=SECTIONS,
        chapters=[
            RecapChapter("arrival", "Arrival", dates=["2026-03-01", "2026-03-02"]),
            RecapChapter("water", "Water", sections=["water"]),
            RecapChapter("main", "Main"),
        ],
    )


def test_date_range_wins_over_section():
    """The day something happened beats what happens to be in the frame."""
    assert recap.chapter_for("2026-03-01T09:00:00", "water", _config()) == "arrival"


def test_section_assigns_when_no_date_matches():
    assert recap.chapter_for("2026-04-01T09:00:00", "water", _config()) == "water"


def test_falls_back_to_default_chapter():
    assert recap.chapter_for("2026-04-01T09:00:00", "other", _config()) == "main"


def test_undated_item_still_lands_somewhere():
    assert recap.chapter_for("", "other", _config()) == "main"


def test_no_chapters_yields_empty():
    assert recap.chapter_for("2026-03-01", "water", RecapConfig()) == ""


def _item(order, kind="image", **kwargs):
    return recap.RecapItem(
        order=order,
        id=order,
        filename=f"{order}.jpg",
        path=Path(f"{order}.jpg"),
        owner_name=kwargs.pop("person", "Alex"),
        export_kind=kind,
        person=kwargs.pop("person_name", "Alex"),
        **kwargs,
    )


def test_clip_length_is_capped():
    item = _item(1, "video", duration_s=60.0)
    assert recap.item_duration(item) == recap.MAX_CLIP_SECONDS


def test_short_clip_keeps_its_own_length():
    assert recap.item_duration(_item(1, "video", duration_s=1.2)) == 1.2


def test_consecutive_clips_from_one_person_are_trimmed_harder():
    previous = _item(1, "video", duration_s=10.0)
    current = _item(2, "video", duration_s=10.0)
    assert recap.item_duration(current, previous) == recap.REPEAT_CLIP_SECONDS


def test_a_burst_of_similar_stills_gets_less_time_each():
    previous = _item(1, tags=["beach", "sunset"], section="water", chapter="c")
    current = _item(2, tags=["beach", "sunset"], section="water", chapter="c")
    assert recap.item_duration(current, previous) == recap.SIMILAR_STILL_SECONDS


def test_a_distinct_still_holds_longer():
    previous = _item(1, tags=["beach"], section="water", chapter="c")
    current = _item(9, tags=["jungle"], section="forest", chapter="d", person_name="Blair")
    assert recap.item_duration(current, previous) == recap.STILL_SECONDS


def test_evenly_spaced_samples_across_the_range():
    items = [_item(i) for i in range(10)]
    picked = recap.evenly_spaced(items, 3)
    assert len(picked) == 3
    assert picked[0].order == 0
    assert picked[-1].order > picked[0].order


def test_evenly_spaced_returns_everything_when_asked_for_more_than_exists():
    items = [_item(i) for i in range(3)]
    assert len(recap.evenly_spaced(items, 10)) == 3


def test_evenly_spaced_handles_zero():
    assert recap.evenly_spaced([_item(1)], 0) == []


def test_selection_respects_the_chapter_budget():
    config = RecapConfig(chapters=[RecapChapter("main", "Main", budget=10.0)])
    items = [_item(i, chapter="main", tags=[f"t{i}"]) for i in range(1, 60)]
    selected = recap.select_items(items, config)

    total, previous = 0.0, None
    for item in selected:
        total += recap.item_duration(item, previous)
        previous = item

    assert selected
    assert total <= config.chapters[0].budget + 2.0


def test_selection_keeps_first_and_last_as_bookends():
    config = RecapConfig(chapters=[RecapChapter("main", "Main", budget=40.0)])
    items = [_item(i, chapter="main", tags=[f"t{i}"]) for i in range(1, 30)]
    orders = [item.order for item in recap.select_items(items, config)]
    assert orders[0] == 1
    assert orders[-1] == 29


def test_selection_preserves_order():
    config = RecapConfig(chapters=[RecapChapter("main", "Main", budget=30.0)])
    items = [_item(i, chapter="main") for i in range(1, 40)]
    orders = [item.order for item in recap.select_items(items, config)]
    assert orders == sorted(orders)
    assert len(orders) == len(set(orders))


def test_empty_chapters_are_skipped():
    config = RecapConfig(chapters=[RecapChapter("nothing", "Nothing", budget=30.0)])
    assert recap.select_items([_item(1, chapter="elsewhere")], config) == []
