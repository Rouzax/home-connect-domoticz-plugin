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
