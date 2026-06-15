"""UserDocumentsStore multi-slot pay-stub semantics."""

from __future__ import annotations

import pytest

from nac_pay.storage import DocumentKind, UserDocumentsStore, get_data_dir


def _store(uid: str = "user-stub-tests") -> UserDocumentsStore:
    return UserDocumentsStore(get_data_dir(), uid)


def test_save_stub_assigns_increasing_slots():
    store = _store()
    r1 = store.save_stub(2026, 5, "stub1.pdf", b"data1")
    r2 = store.save_stub(2026, 5, "stub2.pdf", b"data2")
    r3 = store.save_stub(2026, 5, "stub3.pdf", b"data3")
    assert (r1.slot, r2.slot, r3.slot) == (0, 1, 2)
    assert r1.path != r2.path != r3.path


def test_list_stubs_orders_by_slot():
    store = _store("user-list-stubs")
    store.save_stub(2026, 6, "second.pdf", b"two")
    store.save_stub(2026, 6, "first.pdf", b"one")
    stubs = store.list_stubs(2026, 6)
    assert [s.slot for s in stubs] == [0, 1]
    assert [s.original_filename for s in stubs] == ["second.pdf", "first.pdf"]


def test_list_stubs_isolated_per_month():
    store = _store("user-isolation")
    store.save_stub(2026, 5, "may.pdf", b"may")
    store.save_stub(2026, 6, "jun.pdf", b"jun")
    assert len(store.list_stubs(2026, 5)) == 1
    assert len(store.list_stubs(2026, 6)) == 1
    assert store.list_stubs(2026, 7) == []


def test_list_stubs_isolated_per_user():
    s1 = _store("user-a")
    s2 = _store("user-b")
    s1.save_stub(2026, 5, "a.pdf", b"aa")
    assert len(s1.list_stubs(2026, 5)) == 1
    assert s2.list_stubs(2026, 5) == []


def test_delete_stub_removes_one_slot_only():
    store = _store("user-delete-stub")
    store.save_stub(2026, 5, "a.pdf", b"a")
    store.save_stub(2026, 5, "b.pdf", b"b")
    store.save_stub(2026, 5, "c.pdf", b"c")

    assert store.delete_stub(2026, 5, slot=1) is True
    remaining = store.list_stubs(2026, 5)
    assert [s.slot for s in remaining] == [0, 2]
    assert [s.original_filename for s in remaining] == ["a.pdf", "c.pdf"]


def test_delete_stub_does_not_renumber():
    """Slot numbers are stable so existing links/handles stay valid."""
    store = _store("user-stable-slots")
    store.save_stub(2026, 5, "x.pdf", b"x")
    store.save_stub(2026, 5, "y.pdf", b"y")
    store.delete_stub(2026, 5, slot=0)
    next_one = store.save_stub(2026, 5, "z.pdf", b"z")
    # New stub gets slot 2 (max+1), NOT slot 0 (reuse) — slots monotonic.
    assert next_one.slot == 2


def test_save_rejects_pay_stub_kind():
    store = _store("user-save-rejects-stub")
    with pytest.raises(ValueError, match="save_stub"):
        store.save(2026, 5, DocumentKind.PAY_STUB, "x.pdf", b"x")


def test_get_rejects_pay_stub_kind():
    store = _store("user-get-rejects-stub")
    with pytest.raises(ValueError, match="list_stubs"):
        store.get(2026, 5, DocumentKind.PAY_STUB)


def test_delete_rejects_pay_stub_kind():
    store = _store("user-delete-rejects-stub")
    with pytest.raises(ValueError, match="delete_stub"):
        store.delete(2026, 5, DocumentKind.PAY_STUB)


def test_list_all_includes_pay_stubs_and_single_kinds():
    store = _store("user-list-all-mixed")
    store.save(2026, 5, DocumentKind.FINAL_AWARD, "fa.pdf", b"fa")
    store.save_stub(2026, 5, "s1.pdf", b"s1")
    store.save_stub(2026, 5, "s2.pdf", b"s2")
    rows = store.list_all()
    kinds = sorted(r.kind.value for r in rows)
    assert kinds == ["FINAL_AWARD", "PAY_STUB", "PAY_STUB"]


def test_available_months_picks_up_pay_stub_only_months():
    store = _store("user-stub-only-month")
    store.save_stub(2026, 7, "july.pdf", b"jul")
    assert (2026, 7) in store.available_months()


def test_files_persist_at_slotted_paths():
    """Disk bytes correspond to slot-numbered filenames."""
    store = _store("user-disk-layout")
    r0 = store.save_stub(2026, 5, "first.pdf", b"AAA")
    r1 = store.save_stub(2026, 5, "second.pdf", b"BBB")
    assert r0.path.name == "stub_0.pdf"
    assert r1.path.name == "stub_1.pdf"
    assert r0.path.read_bytes() == b"AAA"
    assert r1.path.read_bytes() == b"BBB"
