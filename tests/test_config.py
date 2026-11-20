from flavrbot import config


def test_defaults_and_problems():
    s = config.load({})
    assert s.listen_port == 3870 and s.quiet_hours == (3, 11)
    assert len(config.problems(s)) == 3
    s = config.load({"DISCORD_TOKEN": "t", "OWNER_ID": "12", "FLAVR_CORE_KEY": "k", "QUIET_HOURS": "off",
                     "PROMO_URL": "http://127.0.0.1:3871/"})
    assert config.problems(s) == []
    assert s.quiet_hours == (0, 0)
    assert s.promo_url == "http://127.0.0.1:3871"       # FlaVR Promo's address, trailing / dropped
    assert config.load({}).promo_url == ""
    assert config.load({"QUIET_HOURS": "22-6"}).quiet_hours == (22, 6)
