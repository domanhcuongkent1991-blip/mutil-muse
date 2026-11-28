# -*- coding: utf-8 -*-
"""Sua luat danh thuc 28/11: mentions dich danh wake khi khong co
to_account; tin truc tiep 1-1 khong bi vong dong chan; nguoi mo
vong gui tin moi thi mo vong moi."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.normpath(os.path.join(HERE, "..", "..", "muse-side"))
sys.path.insert(0, CODE)

import center_dispatcher
import center_discussion


def _ctx(rounds=None):
    return {"replied_up_to_seq": 0, "source_msg_ids": set(),
            "discussion_rounds": rounds or {},
            "participating": True, "discussion_enabled": True}


def _rec(**kw):
    rec = {"kind": "hoi", "from_account": "muse-1996",
           "room": "chung", "msg_id": "m-x"}
    rec.update(kw)
    return rec


class WakeMentionsTest(unittest.TestCase):
    def test_mention_wakes_when_no_to_account(self):
        rec = _rec(to_account=None, mentions=["muse-chinh"])
        self.assertEqual(
            center_dispatcher.check_chat_wake(
                rec, 10, "muse-chinh", _ctx()), [])

    def test_mention_ignored_when_to_account_lists_others(self):
        rec = _rec(to_account=["muse-1992-a"],
                   mentions=["muse-chinh"])
        self.assertIn(
            "W4", center_dispatcher.check_chat_wake(
                rec, 10, "muse-chinh", _ctx()))

    def test_no_mention_no_wake(self):
        rec = _rec(to_account=None, mentions=[])
        self.assertIn(
            "W4", center_dispatcher.check_chat_wake(
                rec, 10, "muse-chinh", _ctx()))


class WakeRoundTest(unittest.TestCase):
    def _closed_rounds(self):
        key = center_discussion.discussion_key(
            "chung", "muse-chinh",
            ["muse-1992-a", "muse-1996", "muse-buikhatnhu",
             "muse-chinh"])
        return {key: {"count": 5, "state": "dong",
                      "opened_seq": 1, "last_seq": 9}}

    def test_direct_single_not_blocked_by_closed_round(self):
        rec = _rec(to_account=["muse-chinh"])
        self.assertNotIn(
            "W7", center_dispatcher.check_chat_wake(
                rec, 10, "muse-chinh", _ctx(self._closed_rounds())))

    def test_group_continuation_still_blocked(self):
        rec = _rec(to_account=["muse-chinh", "muse-1992-a"])
        self.assertIn(
            "W7", center_dispatcher.check_chat_wake(
                rec, 10, "muse-chinh", _ctx(self._closed_rounds())))

    def test_opener_message_reopens_closed_round(self):
        msgs = []
        for i in range(5):
            msgs.append({"seq": i + 1, "record": _rec(
                from_account="muse-chinh",
                to_account=["muse-1996", "muse-1992-a",
                            "muse-buikhatnhu"])})
        msgs.append({"seq": 6, "record": _rec(
            from_account="muse-chinh",
            to_account=["muse-1996", "muse-1992-a",
                        "muse-buikhatnhu"])})
        rounds = center_discussion.compute_discussion_rounds(msgs)
        values = list(rounds.values())
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["count"], 1)
        self.assertEqual(values[0]["state"], "mo")
        self.assertEqual(values[0]["opened_seq"], 6)


if __name__ == "__main__":
    unittest.main()
