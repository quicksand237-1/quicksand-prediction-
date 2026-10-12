import unittest

from strategy_simulator import paper_position_rebalance, select_mandatory_outcomes, settle_paper_positions, mandatory_probability_weights


def book(label="180-199", lower=180, probability=.6, ask=.2, bid=.19, safe=True):
    return dict(outcome_label=label, lower_count=lower, upper_count=lower+19,
                model_probability=probability, best_ask=ask, best_bid=bid,
                ask_depth_5=10000, bid_depth_5=10000, point_in_time_safe=safe,
                paper_net_edge_after_cost=probability-ask-.01, entry_allowed=False,
                book_rejection_reason="missing_orderbook_snapshot" if not safe else "safe")


class MandatoryPaperTests(unittest.TestCase):
    def test_hold_to_settlement_ignores_all_exits_and_additions(self):
        for bid, edge in [(.01,-.8),(.9,.5),(.5,.02)]:
            row=book(bid=bid,ask=max(.51,bid+.01));row['paper_net_edge_after_cost']=edge;row['entry_allowed']=True
            state={'180-199':{'quantity':10.,'avg_cost':.5,'realized_pnl':0.}}
            account={'cash':95.}
            actions,_,_=paper_position_rebalance([row],state,checkpoint_index=30,paper_budget=100,account=account,risk_exits=True,hold_to_settlement=True)
            self.assertEqual(actions[0]['quantity_delta'],0)
            self.assertEqual(actions[0]['reason'],'hold_to_settlement')
            self.assertEqual(account['cash'],95)
            self.assertEqual(settle_paper_positions(state,account,'180-199')[0]['cash_flow'],10)

    def test_contiguous_tail_exclusion_and_unsorted_input(self):
        rows=[book("500+",500,.2),book("200-219",200,.2),book(),book("160-179",160,.1)]
        self.assertEqual(select_mandatory_outcomes(rows,support_lower=160,support_upper=220),["180-199","160-179","200-219"])

    def test_fractional_probability_budget_examples(self):
        self.assertEqual(mandatory_probability_weights([book(probability=.35)],probability_coverage=.3),{'180-199':1.})
        rows=[book(probability=.25),book('160-179',160,.21),book('200-219',200,.14)]
        weights=mandatory_probability_weights(rows,probability_coverage=.3)
        self.assertAlmostEqual(weights['180-199'],25/30)
        self.assertAlmostEqual(weights['160-179'],.1)
        self.assertAlmostEqual(weights['200-219'],1/15)
        account={'cash':100.}
        actions,_,_=paper_position_rebalance(rows,{},checkpoint_index=0,paper_budget=100,account=account,mandatory_entry=True,probability_coverage=.3)
        for action in actions:
            self.assertAlmostEqual(-action['cash_flow'],100*weights[action['outcome_label']],places=6)

    def test_negative_edge_still_buys_and_conserves_cash(self):
        rows=[book(probability=.1,ask=.5),book("200-219",200,.1,ask=.5)]
        account={"cash":100.}
        positions={}
        actions,_,_=paper_position_rebalance(rows,positions,checkpoint_index=30,paper_budget=100,account=account,mandatory_entry=True)
        self.assertTrue(all(a["quantity_delta"]>0 for a in actions))
        self.assertAlmostEqual(sum(-a["cash_flow"] for a in actions),25,places=6)
        self.assertAlmostEqual(account["cash"],75,places=6)

    def test_invalid_book_never_fills(self):
        account={"cash":100.}
        actions,_,_=paper_position_rebalance([book(safe=False)],{},checkpoint_index=30,paper_budget=100,account=account,mandatory_entry=True)
        self.assertEqual(actions[0]["quantity_delta"],0)
        self.assertEqual(actions[0]["action"],"no_fill")
        self.assertNotIn("mandatory_entry_complete",account)

    def test_exit_reason_and_per_sale_profit(self):
        for bid,edge,reason in [(.7,.3,"take_profit"),(.3,-.1,"stop_loss"),(.5,-.1,"exit_edge_below_zero"),(.5,.02,"reduce_below_retain")]:
            with self.subTest(reason=reason):
                row=book(bid=bid,ask=max(.51,bid+.01));row["paper_net_edge_after_cost"]=edge
                state={"180-199":{"quantity":10.,"avg_cost":.5,"realized_pnl":2.}}
                account={"cash":95.}
                actions,pnl,_=paper_position_rebalance([row],state,checkpoint_index=30,paper_budget=100,account=account,risk_exits=True)
                a=actions[0]
                self.assertEqual(a["reason"],reason)
                self.assertAlmostEqual(a["realized_pnl_delta"],pnl,places=7)
                self.assertAlmostEqual(a["realized_pnl"],2+pnl,places=7)
                self.assertAlmostEqual(account["cash"],95+a["cash_flow"],places=7)

    def test_legacy_has_no_profit_exit(self):
        row=book(bid=.7,ask=.71);row["paper_net_edge_after_cost"]=.06
        a,_,_=paper_position_rebalance([row],{"180-199":{"quantity":10.,"avg_cost":.5}},checkpoint_index=30,paper_budget=100)
        self.assertEqual(a[0]["quantity_delta"],0)

    def test_partial_fill_and_settlement(self):
        row=book();row["ask_depth_5"]=2
        state={}; account={"cash":100.}
        a,_,_=paper_position_rebalance([row],state,checkpoint_index=30,paper_budget=100,account=account,mandatory_entry=True)
        self.assertTrue(a[0]["partial_fill"])
        self.assertEqual(settle_paper_positions(state,account,None),[])
        result=settle_paper_positions(state,account,"180-199")
        self.assertEqual(result[0]["cash_flow"],2)
        self.assertAlmostEqual(account["cash"]-100,result[0]["realized_pnl_delta"])
        self.assertEqual(settle_paper_positions(state,account,"180-199"),[])


if __name__ == "__main__":
    unittest.main()
