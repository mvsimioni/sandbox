from src import dpa_soa as d


def test_kpu_normalised():
    assert d.norm_kpu("KPU 10") == "KPU10" and d.norm_kpu("KPU11 ") == "KPU11" and d.norm_kpu("10") == "KPU10"
    assert d.norm_kpu("KPU07- NECE") == "KPU07 NECE" and d.norm_kpu("IHPCS") == "IHPCS"


def test_storey_only_from_storey_based_identifiers():
    assert d.storey_of("LO SFLABM.LB3.04", "SFLABM") == "LB3"
    assert d.storey_of("LL FLLIP.05.06", "FLLIP") == "L05"
    assert d.storey_of("IWPR2 05.02.01", "05") == ""          # group and sub-group numbers, not a storey
    assert d.storey_of("ST FSTOP.01.SB.30", "FSTOP") == ""    # unrecognised tail stays blank
