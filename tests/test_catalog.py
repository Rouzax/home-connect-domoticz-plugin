import auth
import catalog


def test_humanize():
    assert catalog.humanize("BSH.Common.Option.RemainingProgramTime") == "Remaining program time"
    assert catalog.humanize("LaundryCare.Washer.Setting.IDos1BaseLevel") == "Idos1 base level"


def test_name_prefers_catalog_then_api_name_then_humanized():
    key = "Cooking.Oven.Status.CurrentCavityTemperature"
    assert catalog.name_for({}, key, "nl") == "Temperatuur ovenruimte"
    assert catalog.name_for({key: "Binnenruimte"}, key, "nl") == "Temperatuur ovenruimte"
    assert catalog.name_for({"A.B.Option.Odd": "Odd"}, "A.B.Option.Odd", "nl") == "Odd"
    assert catalog.name_for({}, "A.B.Option.SpinSpeed", "en") == "Spin speed"


def test_fallback_names_cover_both_languages():
    key = "LaundryCare.Washer.Option.SpinSpeed"
    assert catalog.name_for({}, key, "nl") == "Centrifugetoerental"
    assert catalog.name_for({}, key, "en") == "Spin speed"
    assert catalog.name_for({key: "Toerental"}, key, "nl") == "Centrifugetoerental"


def test_known_keys():
    assert catalog.known(catalog.OPERATION_STATE)
    assert catalog.known("Cooking.Oven.Status.CurrentCavityTemperature")
    assert catalog.known("BSH.Common.Setting.ChildLock")
    assert catalog.known("BSH.Common.Event.Favorite.001.ExternalTrigger")
    assert not catalog.known("X.Y.Status.Thing")


def test_colours():
    assert catalog.STATE_COLOUR["ActionRequired"] == catalog.ORANGE
    assert catalog.STATE_COLOUR["Error"] == catalog.RED
    assert catalog.STATE_COLOUR["DelayedStart"] == catalog.GREEN
    assert catalog.EVENT_LEVEL_COLOUR["info"] == catalog.GREY
    assert catalog.EVENT_LEVEL_COLOUR["alert"] == catalog.RED


def test_words_have_both_languages():
    assert catalog.word("offline", "en") == "Offline"
    assert catalog.word("last_known", "nl") == "laatst bekend"
    assert catalog.state_name("Run", "nl") == "Bezig"


def test_catalog_name_is_only_the_catalogs_own():
    cavity = "Cooking.Oven.Status.CurrentCavityTemperature"
    assert catalog.catalog_name(cavity, "en") == "Cavity temperature"
    assert catalog.catalog_name("Cooking.Oven.Option.SetpointTemperature", "nl") == (
        "Ingestelde temperatuur"
    )
    assert catalog.catalog_name("Vendor.Status.Unknown", "en") is None
    assert catalog.name_for({}, "Vendor.Status.UnknownThing", "en") == "Unknown thing"


def test_catalog_names_are_unique_within_a_domain():
    # Keys of one domain (plus the BSH.Common keys every appliance has) can share a device, and
    # alike names would get a " (2)" suffix that depends on unit order.
    keys = [*catalog._NAMES, *catalog._SPECIAL]
    domains = {k.split(".")[0] for k in keys} - {"BSH"}
    for lang in ("en", "nl"):
        for domain in domains:
            names = [
                str(catalog.catalog_name(k, lang)).casefold()
                for k in keys
                if k.split(".")[0] in (domain, "BSH")
            ]
            assert len(names) == len(set(names)), (lang, domain)


def test_delay_options_are_named_like_their_unit():
    for key, en, nl in (
        (catalog.START_IN, "Start at", "Start om"),
        (catalog.FINISH_IN, "Ready at", "Klaar om"),
    ):
        assert catalog.name_for({}, key, "en") == en
        assert catalog.name_for({}, key, "nl") == nl


def test_english_leftovers_have_dutch_names():
    for key, nl in (
        ("Cooking.Oven.Event.EcolysisRequired", "Ecolysis-reiniging nodig"),
        ("BSH.Common.Option.SmartEnergyService.SmartStartEnabled", "Slimme start"),
    ):
        assert catalog.name_for({key: "English name"}, key, "nl") == nl
        assert catalog.known(key)


def test_catalog_corrects_home_connect_typos_in_program_names():
    for key, sent, nl in (
        ("LaundryCare.Dryer.Program.Mix", "MIx", "Mix"),
        ("LaundryCare.Dryer.Program.Super40", "Super Quick 40`", "Super Quick 40"),
        ("LaundryCare.Dryer.Program.InBasket", "Wol in droofkorf", "Wol in droogkorf"),
    ):
        assert catalog.name_for({key: sent}, key, "nl") == nl


def test_value_names_win_over_english_and_fill_numbered_colours():
    custom = "BSH.Common.EnumType.AmbientLightColor.CustomColor"
    assert catalog.value_name({custom: "Custom color"}, custom, "nl") == "Eigen kleur"
    assert catalog.value_name({}, custom, "en") == "Custom colour"
    colour = "BSH.Common.EnumType.AmbientLightColor.Color30"
    assert catalog.value_name({}, colour, "nl") == "Kleur 30"
    assert catalog.value_name({}, colour, "en") == "Colour 30"
    named = "BSH.Common.EnumType.AmbientLightColor.Color40"
    assert catalog.value_name({named: "koud wit"}, named, "nl") == "koud wit"
    assert catalog.value_name({}, "X.Y.EnumType.Thing.Color3", "nl") is None
    assert catalog.value_name({}, "X.Y.EnumType.Thing.Odd", "nl") is None


def test_units_of_measure_are_translated():
    assert catalog.unit_text("calls", "nl") == "aanroepen"
    assert catalog.unit_text("°C", "nl") == "°C"


def test_every_sign_in_state_has_plain_words_for_the_log():
    for state in auth.AuthState:
        words = catalog.sign_in_state(state.value)
        assert words[0].islower() and "_" not in words and state.value not in words, state
    assert (
        catalog.sign_in_state("NEEDS_USER_ACTION") == "action needed in your Home Connect account"
    )


def test_account_notes_use_the_sign_in_wording():
    assert catalog.note("auth_exhausted", "en") == "Sign-in budget used up; try later"
    assert catalog.note("auth_exhausted", "nl") == "Aanmeldbudget op; probeer later"
    assert catalog.note("denied", "en") == "Sign-in denied"
    assert catalog.note("denied", "nl") == "Aanmelding geweigerd"


def test_dishwasher_options_use_the_home_connect_app_names():
    for key, en, nl in (
        ("Dishcare.Dishwasher.Option.SilenceOnDemand", "Silence on demand", "Geluid dempen"),
        ("Dishcare.Dishwasher.Option.BrillianceDry", "Brilliance Dry", "GlansDroog"),
        ("Dishcare.Dishwasher.Option.VarioSpeedPlus", "VarioSpeed Plus", "VarioSpeedPlus"),
        ("Dishcare.Dishwasher.Option.HygienePlus", "Hygiene Plus", "HygiënePlus"),
        ("Dishcare.Dishwasher.Option.IntensivZone", "Intensive Zone", "IntensiveZone"),
    ):
        assert catalog.name_for({}, key, "en") == en
        assert catalog.name_for({}, key, "nl") == nl


def test_meters_name_their_quantity_in_both_languages():
    water = catalog.meter("BSH.Common.Status.Program.All.Water.Consumed")
    assert water is not None
    assert (water.kind, water.units, water.divisor) == ("counter_total", "L", 1000)
    assert water.quantity == ("Water", "Water")
    assert water.reported == "ml"
    energy = catalog.meter("BSH.Common.Status.Program.All.Energy.Consumed")
    assert energy is not None and energy.kind == "energy_total"
    assert energy.reported == "Wh"
    assert catalog.meter("BSH.Common.Status.Program.All.Count.Completed") is None
    assert catalog.pick(("Detergent", "Wasmiddel"), "nl") == "Wasmiddel"
