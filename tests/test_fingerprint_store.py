

def test_clean_title_strips_markup_entities_and_one_full_stop():
    from src.fingerprint_store import clean_title
    assert clean_title("Cancer-cell-derived cGAMP limits CD8<sup>+</sup>T cells") == "Cancer-cell-derived cGAMP limits CD8+T cells"
    assert clean_title("Paralogs  <i>ndk</i>  in <i>Waddlia</i>.") == "Paralogs ndk in Waddlia"
    assert clean_title("A &amp; B in cells") == "A & B in cells"
    assert clean_title("Ends in ellipsis...") == "Ends in ellipsis..."
    assert clean_title(None) == "" and clean_title("   ") == ""
