"""路径规则引擎测试。"""

import unittest

from src.domain.enums import DisputeType, NodeCode
from src.domain.rules import ALLOWED_ROUTES, plan_path


class RouteRulesTest(unittest.TestCase):
    def test_labor_dispute_arbitration_preceded(self) -> None:
        plan = plan_path(DisputeType.CREW_LABOR, "labor_arbitration")
        nodes = [s.node for s in plan.segments]
        self.assertEqual(nodes, [NodeCode.INTAKE, NodeCode.LABOR_ARBITRATION,
                                 NodeCode.LITIGATION])
        self.assertIn("仲裁前置", plan.choice_explanation)

    def test_commercial_arbitration_without_clause_warns(self) -> None:
        plan = plan_path(DisputeType.CARGO_TRANSPORT, "commercial_arbitration",
                         has_arbitration_clause=False)
        self.assertTrue(any("仲裁条款" in w for w in plan.warnings))
        plan2 = plan_path(DisputeType.CARGO_TRANSPORT, "commercial_arbitration",
                          has_arbitration_clause=True)
        self.assertEqual(plan2.warnings, ())

    def test_cross_border_inserts_service_stage_before_litigation(self) -> None:
        plan = plan_path(DisputeType.MARITIME_ACCIDENT, "litigation", cross_border=True)
        nodes = [s.node for s in plan.segments]
        self.assertIn(NodeCode.CROSS_BORDER_SERVICE, nodes)
        self.assertLess(nodes.index(NodeCode.CROSS_BORDER_SERVICE),
                        nodes.index(NodeCode.LITIGATION))

    def test_invalid_route_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_path(DisputeType.CREW_LABOR, "not_a_route")

    def test_limitation_basis_present(self) -> None:
        plan = plan_path(DisputeType.CREW_LABOR, next(iter(ALLOWED_ROUTES[DisputeType.CREW_LABOR])))
        self.assertEqual(plan.limitation_days, 365)
        self.assertIn("仲裁时效一年", plan.limitation_basis)


if __name__ == "__main__":
    unittest.main()
