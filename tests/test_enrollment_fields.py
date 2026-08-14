"""
Field-level 834 enrollment tests (no Spark required).

Validates that both accelerator sample files map the v1 field set:
header (BGN/QTY/N1), member REFs/DTPs, INS maintenance, related parties,
plan elections, and LS/LE 2700 reporting categories when present.
"""
import json
import os
import unittest

from ember import EDI
from ember.hls.healthcare import HealthcareManager as hm


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sampledata", "834")


def _load(name):
    path = os.path.join(SAMPLE_DIR, name)
    with open(path, "rb") as f:
        return EDI(f.read().decode("utf-8"), strict_transactions=False)


class TestEnrollmentFields(unittest.TestCase):

    def test_edi_834_classic_addition(self):
        members = hm.from_edi(_load("EDI_834.txt"))
        self.assertEqual(len(members), 1)
        data = members[0].to_json()["enrollment_member"]

        # Header / sponsor
        hdr = data["transaction_header"]
        self.assertEqual(hdr["bgn_purpose_code"], "00")
        self.assertEqual(hdr["bgn_reference_id"], "12456")
        self.assertEqual(hdr["sponsors"][0]["entity_identifier_code"], "P5")
        self.assertEqual(hdr["sponsors"][0]["id_code"], "999888777")
        self.assertEqual(hdr["insurers"][0]["entity_identifier_code"], "IN")
        self.assertEqual(hdr["insurers"][0]["id_code"], "654456654")

        # Member
        self.assertEqual(data["member_last_name"], "DOE")
        self.assertEqual(data["member_first_name"], "JOHN")
        self.assertEqual(data["member_id_code"], "123456789")
        self.assertEqual(data["member_nm1_entity_code"], "IL")
        self.assertEqual(data["address"]["city_name"], "CAMP HILL")
        self.assertEqual(data["address"]["location_qualifier"], "CY")
        self.assertEqual(data["address"]["location_identifier"], "CUMBERLAND")
        self.assertEqual(data["contact_phone_number"]["home_phone"], "7172343334")
        self.assertEqual(data["contact_phone_number"]["work_phone"], "7172341240")

        # REFs
        ref_map = {r["ref_qualifier"]: r["ref_value"] for r in data["references"]}
        self.assertEqual(ref_map["0F"], "123456789")
        self.assertEqual(ref_map["1L"], "123456001")

        # Maintenance
        maint = data["Maintenance"]
        self.assertEqual(maint["maintenance_type_code"], "021")
        self.assertEqual(maint["maintenance_type_desc"], "Addition")
        self.assertEqual(maint["individual_relationship_code"], "18")
        self.assertEqual(maint["individual_relationship_desc"], "Self")
        self.assertEqual(maint["maintenance_reason_code"], "20")
        self.assertEqual(maint["benefit_status_code"], "A")
        self.assertEqual(maint["employment_status_code"], "FT")
        self.assertEqual(maint["eligibility_begin_date"], "19960523")
        self.assertEqual(maint["coverage_start_date"], "19960523")

        # Elections
        elections = data["health_coverage_elections"]
        self.assertEqual(len(elections), 3)
        types = [e["coverage_type_code"] for e in elections]
        self.assertEqual(types, ["HLT", "DEN", "VIS"])
        self.assertEqual(elections[0]["cob_policy_number"], "890111")
        self.assertEqual(elections[0]["coverage_start_dt"], "19960601")

        # No Medicaid reporting loop on this file
        self.assertEqual(data["reporting_categories"], [])

    def test_834_test_medicaid_style(self):
        members = hm.from_edi(_load("834_test.txt"))
        self.assertEqual(len(members), 1)
        data = members[0].to_json()["enrollment_member"]

        hdr = data["transaction_header"]
        self.assertEqual(hdr["qty_qualifier"], "TO")
        self.assertEqual(hdr["qty_value"], "1")
        self.assertEqual(hdr["sponsors"][0]["name"], "MEDICAID")
        self.assertEqual(hdr["sponsors"][0]["id_code"], "141797357")
        self.assertEqual(hdr["insurers"][0]["id_code"], "8-DIGIT PLAN ID")

        self.assertEqual(data["member_nm1_entity_code"], "74")
        self.assertEqual(data["member_last_name"], "SUBSCRIBER B LAST NAME")
        self.assertEqual(data["member_first_name"], "SUBSCRIBER B FIRST NAME")
        self.assertEqual(data["member_id_code"], "299999992")
        self.assertEqual(data["contact_phone_number"]["home_phone"], "9999999999")

        ref_map = {r["ref_qualifier"]: r["ref_value"] for r in data["references"]}
        self.assertEqual(ref_map["0F"], "XX29992X")
        self.assertEqual(ref_map["1L"], "HEALTH PLAN GROUP NUM")
        self.assertEqual(ref_map["17"], "XX29992X")
        self.assertEqual(ref_map["23"], "PLAN ASSIGNED MEMBER ID")
        self.assertEqual(ref_map["ZZ"], "PLAN ASSIGNED SUBSCRIBER ID")

        maint = data["Maintenance"]
        self.assertEqual(maint["maintenance_type_code"], "001")
        self.assertEqual(maint["maintenance_type_desc"], "Change")
        self.assertEqual(maint["maintenance_effective_date"], "20240724")
        self.assertEqual(maint["eligibility_begin_date"], "20240701")
        self.assertEqual(maint["coverage_start_date"], "20240701")  # prefers 356
        self.assertEqual(maint["employment_status_code"], "AC")

        # Related parties (70 + QD) before HD
        entities = {p["entity_identifier_code"]: p for p in data["related_parties"]}
        self.assertIn("70", entities)
        self.assertEqual(entities["70"]["last_name_or_org"], "SUBSCRIBER B1 LAST NAME")
        self.assertEqual(entities["70"]["dob"], "20010101")
        self.assertIn("QD", entities)
        self.assertEqual(entities["QD"]["last_name_or_org"], "CASE NAME")

        elections = data["health_coverage_elections"]
        self.assertEqual(len(elections), 3)
        self.assertEqual(elections[0]["coverage_level_code"], "IND")
        self.assertEqual(elections[0]["coverage_end_dt"], "20241231")
        hlt_refs = {r["ref_qualifier"]: r["ref_value"] for r in elections[0]["references"]}
        self.assertEqual(hlt_refs.get("X9"), "PLAN ASSIGNED POLICY ID")

        # LS/LE 2700 reporting
        cats = data["reporting_categories"]
        self.assertGreaterEqual(len(cats), 10)
        by_name = {c["name"]: c for c in cats}
        self.assertIn("FAM IND", by_name)
        self.assertEqual(by_name["FAM IND"]["references"][0]["ref_value"], "F")
        self.assertIn("AID CAT CODE", by_name)
        self.assertEqual(by_name["AID CAT CODE"]["references"][0]["ref_value"], "91")

        # Y2 MCO should appear in related parties
        self.assertTrue(any(p["entity_identifier_code"] == "Y2" for p in data["related_parties"]))

    def test_ins_count_still_matches(self):
        for name in ("EDI_834.txt", "834_test.txt"):
            edi = _load(name)
            ins = len([s for s in edi.data if s._name == "INS"])
            self.assertEqual(ins, len(hm.from_edi(edi)), msg=name)

    def test_json_serializable(self):
        for name in ("EDI_834.txt", "834_test.txt"):
            payload = hm.from_edi(_load(name))[0].to_json()
            json.dumps(payload)  # must not raise


if __name__ == "__main__":
    unittest.main()
