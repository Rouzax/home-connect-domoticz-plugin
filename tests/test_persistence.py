import persistence


def full_state():
    return persistence.State(
        tokens=persistence.Tokens("acc", "ref", 1_800_000_000.0, "IdentifyAppliance Monitor", "h1"),
        auth_state="AUTHORIZED",
        last_sign_out=False,
        next_unit={"SIEMENS-X-1": 23},
        unit_alloc={"SIEMENS-X-1": {"Cooking.Oven.Option.SetpointTemperature": 20}},
        level_maps={"SIEMENS-X-1": {"21": ["A.B.C", "A.B.D"]}},
        unit_kinds={"SIEMENS-X-1": {"20": "setpoint", "21": "selector"}},
        auto_names={"SIEMENS-X-1:1": "Oven State"},
        caches={
            "SIEMENS-X-1": persistence.ApplianceCache(
                programs=["P1"],
                program_details={"P1": {"options": []}},
                setting_constraints={},
                commands=["BSH.Common.Command.AcknowledgeEvent"],
                negative_keys=["X.Y"],
                refreshed_at=1.0,
                names={"BSH.Common.Option.Duration": "Pas de tijdsduur aan"},
                value_names={"A.B.C": "Hete lucht"},
            )
        },
        ledger={"background": {"29000000": 3}},
        block_until=0.0,
        block_length=0.0,
        account_shown=False,
    )


def test_round_trip():
    state = full_state()
    loaded, fell_back = persistence.loads(persistence.dumps(state))
    assert fell_back is False
    assert loaded == state


def test_empty_and_garbage_fall_back_without_raising():
    for text in ["", "   ", "not json", "[1,2]", "5", "null", '"x"']:
        state, fell_back = persistence.loads(text)
        assert isinstance(state, persistence.State), text
        assert fell_back is True, text
        assert state == persistence.State(), text


def test_unknown_version_with_wrong_shaped_parts_does_not_raise():
    state, fell_back = persistence.loads('{"version": 999, "tokens": 5}')
    assert isinstance(state, persistence.State)
    assert fell_back is False
    assert state.tokens is None


def test_bool_is_not_accepted_as_int():
    text = persistence.dumps(full_state()).replace(
        '"next_unit":{"SIEMENS-X-1":23}', '"next_unit":{"SIEMENS-X-1":true}'
    )
    state, _ = persistence.loads(text)
    assert state.next_unit == {"SIEMENS-X-1": 21}


def test_wrongly_typed_parts_are_dropped_not_crashing():
    text = persistence.dumps(full_state()).replace(
        '"next_unit":{"SIEMENS-X-1":23}', '"next_unit":{"SIEMENS-X-1":"x"}'
    )
    state, fell_back = persistence.loads(text)
    assert fell_back is False
    assert state.unit_alloc  # the rest survives
    # Never below the highest allocated unit (20 in the fixture), so no unit is reissued.
    assert state.next_unit == {"SIEMENS-X-1": 21}


def test_next_unit_is_only_raised_never_lowered():
    text = persistence.dumps(full_state()).replace(
        '"next_unit":{"SIEMENS-X-1":23}', '"next_unit":{"SIEMENS-X-1":5}'
    )
    assert persistence.loads(text)[0].next_unit == {"SIEMENS-X-1": 21}
    state, _ = persistence.loads(persistence.dumps(full_state()))
    assert state.next_unit == {"SIEMENS-X-1": 23}


def test_deeply_nested_input_falls_back():
    state, fell_back = persistence.loads("[" * 200_000)
    assert fell_back is True
    assert state == persistence.State()


def test_non_finite_numbers_load_as_default():
    for literal in ("Infinity", "-Infinity", "NaN"):
        text = persistence.dumps(full_state()).replace(
            '"block_until":0.0', f'"block_until":{literal}'
        )
        assert literal in text
        assert persistence.loads(text)[0].block_until == 0.0


def test_bad_cache_entry_is_dropped_alone():
    text = persistence.dumps(full_state()).replace('"programs":["P1"]', '"programs":"P1"')
    state, _ = persistence.loads(text)
    assert state.caches == {}
    assert state.tokens is not None


def test_tokens_repr_hides_secrets():
    tokens = persistence.Tokens("access-secret", "refresh-secret", 1.0, "s", "h")
    assert "access-secret" not in repr(tokens)
    assert "refresh-secret" not in repr(tokens)


def test_bad_tokens_become_none():
    text = persistence.dumps(full_state()).replace(
        '"expires_at":1800000000.0', '"expires_at":"soon"'
    )
    state, _ = persistence.loads(text)
    assert state.tokens is None


def cache_node(**names):
    return {
        "programs": [],
        "program_details": {},
        "setting_constraints": {},
        "commands": [],
        "negative_keys": [],
        "refreshed_at": 0.0,
        **names,
    }


def load_cache(node):
    import json

    state, fell_back = persistence.loads(json.dumps({"caches": {"SIEMENS-X-1": node}}))
    assert not fell_back
    return state.caches["SIEMENS-X-1"]


def test_garbage_names_are_dropped_and_labels_bounded():
    cache = load_cache(
        cache_node(
            names={
                "Good.Key": "Doel\u202etemperatuur|",
                "bad key": "x",
                "Long.Key": "y" * 500,
                "Num.Key": 5,
                "Empty.Key": "\u0000",
            },
            value_names=["not", "a", "map"],
        )
    )
    assert cache.names == {"Good.Key": "Doel temperatuur", "Long.Key": "y" * 60}
    assert cache.value_names == {}


def test_a_snapshot_without_names_keeps_its_cache():
    cache = load_cache(cache_node())
    assert cache.names == {} and cache.value_names == {}
    assert load_cache(cache_node(names="garbage")).names == {}
