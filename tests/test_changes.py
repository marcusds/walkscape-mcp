import copy

from walkscape_mcp.changes import diff_snapshots, has_changes


def test_snapshot_diff_lists_added_removed_and_changed(gd):
    old = gd.snap
    new = copy.deepcopy(old)
    aid = next(iter(new["activities"]))
    removed_name = new["activities"].pop(aid)["name"]
    new["activities"]["brand_new_activity"] = {**old["activities"][aid], "id": "brand_new_activity",
                                               "name": "Brand new activity"}
    lid = next(iter(new["locations"]))
    new["locations"][lid] = {**new["locations"][lid], "activityList": ["brand_new_activity"]}
    d = diff_snapshots(old, new)
    assert d["added"]["activities"] == ["Brand new activity"]
    assert d["removed"]["activities"] == [removed_name]
    assert new["locations"][lid]["name"] in d["changed"]["locations"]
    assert not has_changes(diff_snapshots(old, copy.deepcopy(old)))
