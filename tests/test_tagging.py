from videoorganizer import tagging


def test_extracts_plain_json():
    parsed = tagging.extract_json_object('{"tags": ["beach"], "description": "A beach."}')
    assert parsed["tags"] == ["beach"]


def test_extracts_from_markdown_fence():
    raw = '```json\n{"tags": ["a"], "description": "d"}\n```'
    assert tagging.extract_json_object(raw)["tags"] == ["a"]


def test_extracts_despite_surrounding_chatter():
    raw = 'Sure! Here is the JSON:\n{"tags": ["a"], "description": "d"}\nHope that helps!'
    assert tagging.extract_json_object(raw)["description"] == "d"


def test_stops_at_the_first_complete_object():
    """A model that emits two objects should not produce a parse error."""
    raw = '{"tags": ["a"], "description": "d"} {"tags": ["b"]}'
    assert tagging.extract_json_object(raw)["tags"] == ["a"]


def test_handles_nested_objects():
    raw = '{"tags": ["a"], "meta": {"nested": {"deep": 1}}, "description": "d"}'
    assert tagging.extract_json_object(raw)["description"] == "d"


def test_braces_inside_strings_do_not_end_the_object():
    raw = '{"tags": ["a"], "description": "a sign reading } closed"}'
    assert tagging.extract_json_object(raw)["description"] == "a sign reading } closed"


def test_escaped_quote_inside_string():
    raw = '{"tags": [], "description": "she said \\"hi\\" loudly"}'
    assert tagging.extract_json_object(raw)["description"] == 'she said "hi" loudly'


def test_returns_none_for_unusable_input():
    assert tagging.extract_json_object("I cannot describe this image.") is None
    assert tagging.extract_json_object("") is None
    assert tagging.extract_json_object("{not valid json") is None


def test_normalize_lowercases_and_dedupes_tags():
    tags, description = tagging.normalize_result(
        {"tags": ["Beach", "beach", " SUNSET "], "description": "  A sunset.  "}
    )
    assert tags == ["beach", "sunset"]
    assert description == "A sunset."


def test_normalize_accepts_a_comma_string_of_tags():
    tags, _ = tagging.normalize_result({"tags": "beach, sunset", "description": "d"})
    assert tags == ["beach", "sunset"]


def test_normalize_rejects_a_wholly_empty_reply():
    assert tagging.normalize_result({"tags": [], "description": ""}) is None
    assert tagging.normalize_result(None) is None


def test_normalize_keeps_a_description_with_no_tags():
    tags, description = tagging.normalize_result({"tags": [], "description": "A beach."})
    assert tags == []
    assert description == "A beach."


def test_prompt_includes_the_configured_subject(library):
    library.tagging.subject = "a mountaineering expedition"
    prompt = tagging.build_prompt(library.tagging, "video", "Alex")
    assert "a mountaineering expedition" in prompt
    assert "video frame" in prompt
    assert "Alex" in prompt


def test_prompt_omits_unknown_contributor(library):
    assert "contributed by" not in tagging.build_prompt(library.tagging, "image", "Unknown")
