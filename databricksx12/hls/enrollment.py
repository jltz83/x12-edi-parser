from databricksx12.edi import EDI, AnsiX12Delim, Segment
from databricksx12.hls.claim import MedicalClaim


class MemberEnrollment(MedicalClaim):
    """
    HIPAA 834 Benefit Enrollment and Maintenance (005010X220A1).

    Grain: one MemberEnrollment per INS (member) loop.
    Optional transaction_header_loop carries BGN / QTY / sponsor-payer N1
    segments that appear before the first INS.
    """

    NAME = "834"

    # NM1 entity identifiers treated as the member/subscriber name
    MEMBER_NM1_QUALIFIERS = ("IL", "74", "1")

    IDENTIFIER_TYPE_MAPPING = {
        "34": "Social Security Number (SSN)",
        "ZZ": "Mutually Defined Identifier",
        "XX": "National Provider Identifier (NPI)",
        "FI": "Federal Tax ID",
        "EI": "Employer ID",
        "MI": "Member ID",
        "SY": "Social Security Number (Alt)",
        "NI": "National Insurance Number",
        "94": "Plan ID",
        "SV": "Service Provider Number",
    }

    COVERAGE_DESC_MAPPING = {
        "HLT": "Health",
        "DEN": "Dental",
        "VIS": "Vision",
    }

    MAINTENANCE_TYPE_MAPPING = {
        "001": "Change",
        "002": "Delete",
        "021": "Addition",
        "024": "Cancellation / Termination",
        "025": "Reinstatement",
        "030": "Audit / Compare",
    }

    RELATIONSHIP_MAPPING = {
        "18": "Self",
        "01": "Spouse",
        "19": "Child",
        "G8": "Other Relationship",
    }

    # Common 834 REF qualifiers (member / policy / group)
    REF_QUALIFIER_MAPPING = {
        "0F": "Subscriber Number",
        "1L": "Group / Policy Number",
        "17": "Client Reporting Category",
        "23": "Client Number",
        "3H": "Case Number",
        "ABB": "Personal ID Number",
        "ZZ": "Mutually Defined",
        "X9": "Policy Number",
        "9V": "Payment Category",
    }

    N1_ENTITY_MAPPING = {
        "P5": "Plan Sponsor",
        "IN": "Insurer",
        "75": "Participant",
    }

    NM1_ENTITY_MAPPING = {
        "IL": "Insured / Subscriber",
        "74": "Corrected Insured",
        "70": "Prior Incorrect Insured",
        "QD": "Responsible Party",
        "Y2": "Managed Care Organization",
        "1": "Person",
    }

    # Member-level DTP qualifiers (not plan HD dates)
    MEMBER_DTP_MAPPING = {
        "303": "Maintenance Effective",
        "356": "Eligibility Begin",
        "357": "Eligibility End",
        "338": "Medicare Begin",
        "339": "Medicare End",
        "340": "COBRA Begin",
        "341": "COBRA End",
        "342": "Premium Paid To Date Begin",
        "343": "Premium Paid To Date End",
        "300": "Enrollment Signature Date",
        "301": "Issue / Recertification",
    }

    def __init__(self, member_detail_loop, transaction_header_loop=None):
        self.member_detail_loop = member_detail_loop or []
        self.transaction_header_loop = transaction_header_loop or []
        self.enrollment_data = self.build_enrollment_data()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _index_segments(segments):
        segment_index = {}
        for x in segments:
            segment_index.setdefault(x._name, []).append(x)
        return segment_index

    @staticmethod
    def _get_first(segment_index, name):
        return segment_index.get(name, [Segment.empty()])[0]

    @staticmethod
    def _get_dtp(segment_index, qualifier):
        found = [x for x in segment_index.get("DTP", []) if x.element(1) == qualifier]
        return found[0] if found else Segment.empty()

    @classmethod
    def _ref_list(cls, segments):
        refs = []
        for seg in segments:
            if seg._name != "REF":
                continue
            qual = seg.element(1)
            refs.append(
                {
                    "ref_qualifier": qual,
                    "ref_qualifier_desc": cls.REF_QUALIFIER_MAPPING.get(qual, qual),
                    "ref_value": seg.element(2),
                    "ref_description": seg.element(3),
                }
            )
        return refs

    @classmethod
    def _dtp_list(cls, segments, mapping=None):
        mapping = mapping or {}
        out = []
        for seg in segments:
            if seg._name != "DTP":
                continue
            qual = seg.element(1)
            out.append(
                {
                    "dtp_qualifier": qual,
                    "dtp_qualifier_desc": mapping.get(qual, qual),
                    "dtp_format": seg.element(2),
                    "dtp_date": seg.element(3),
                }
            )
        return out

    def _find_member_nm1(self, segments):
        nm1s = [s for s in segments if s._name == "NM1"]
        for qual in self.MEMBER_NM1_QUALIFIERS:
            for seg in nm1s:
                if seg.element(1) == qual:
                    return seg
        return nm1s[0] if nm1s else Segment.empty()

    def _parse_contact_phones(self, per):
        """
        PER communication numbers are qualifier/value pairs starting at element 3.
        Supports HP/TE (home/telephone), WP (work), EM (email), etc.
        """
        phones = {
            "home_phone": "",
            "work_phone": "",
            "other_phone": "",
            "email": "",
        }
        # Walk PER03/PER04, PER05/PER06, PER07/PER08
        for i in range(3, 9, 2):
            qual = per.element(i)
            val = per.element(i + 1)
            if not qual:
                continue
            if qual in ("HP", "TE"):
                if not phones["home_phone"]:
                    phones["home_phone"] = val
                else:
                    phones["other_phone"] = val
            elif qual == "WP":
                phones["work_phone"] = val
            elif qual == "EM":
                phones["email"] = val
            elif not phones["other_phone"]:
                phones["other_phone"] = val
        return phones

    def _parse_n1_party(self, n1):
        entity = n1.element(1)
        id_qual = n1.element(3)
        return {
            "entity_identifier_code": entity,
            "entity_identifier_desc": self.N1_ENTITY_MAPPING.get(entity, entity),
            "name": n1.element(2),
            "id_code_qualifier": id_qual,
            "id_code_qualifier_desc": self.IDENTIFIER_TYPE_MAPPING.get(id_qual, id_qual),
            "id_code": n1.element(4),
        }

    def _parse_nm1_person(self, nm1, dmg=None):
        entity = nm1.element(1)
        id_qual = nm1.element(8)
        person = {
            "entity_identifier_code": entity,
            "entity_identifier_desc": self.NM1_ENTITY_MAPPING.get(entity, entity),
            "entity_type": nm1.element(2),  # 1=person, 2=non-person
            "last_name_or_org": nm1.element(3),
            "first_name": nm1.element(4),
            "middle_name": nm1.element(5),
            "id_code_qualifier": id_qual,
            "id_code_qualifier_desc": self.IDENTIFIER_TYPE_MAPPING.get(id_qual, id_qual),
            "id_code": nm1.element(9),
        }
        if dmg is not None and dmg._name == "DMG":
            person["dob"] = dmg.element(2)
            person["gender"] = dmg.element(3)
        return person

    def build_transaction_header(self):
        segs = self.transaction_header_loop
        if not segs:
            return {}

        idx = self._index_segments(segs)
        bgn = self._get_first(idx, "BGN")
        qty = self._get_first(idx, "QTY")

        sponsors = []
        insurers = []
        other_parties = []
        for n1 in idx.get("N1", []):
            party = self._parse_n1_party(n1)
            if n1.element(1) == "P5":
                sponsors.append(party)
            elif n1.element(1) == "IN":
                insurers.append(party)
            else:
                other_parties.append(party)

        header = {
            "bgn_purpose_code": bgn.element(1),
            "bgn_reference_id": bgn.element(2),
            "bgn_date": bgn.element(3),
            "bgn_time": bgn.element(4),
            "bgn_action_code": bgn.element(8),
            "qty_qualifier": qty.element(1),
            "qty_value": qty.element(2),
            "sponsors": sponsors,
            "insurers": insurers,
        }
        if other_parties:
            header["other_parties"] = other_parties
        return header

    def build_plan_elections(self, segments):
        # Truncate election loop before reporting / nested LX that are not HD children
        trimmed = []
        for seg in segments:
            if seg._name in ("LS", "LE") or (
                seg._name == "LX" and trimmed and trimmed[0]._name == "HD"
            ):
                # LX after HD content ends the plan election for v1
                # (Medicaid files put reporting LX after all HD loops)
                break
            trimmed.append(seg)
        if not trimmed:
            trimmed = segments

        segment_index = self._index_segments(trimmed)
        hd = trimmed[0] if trimmed else Segment.empty()

        dtp_348 = self._get_dtp(segment_index, "348")
        dtp_349 = self._get_dtp(segment_index, "349")
        dtp_344 = self._get_dtp(segment_index, "344")
        dtp_345 = self._get_dtp(segment_index, "345")
        cob = self._get_first(segment_index, "COB")

        coverage_type = hd.element(3)
        return {
            "maintenance_type_code": hd.element(1),
            "coverage_type_code": coverage_type,
            "coverage_desc": self.COVERAGE_DESC_MAPPING.get(coverage_type, "Unknown"),
            "plan_coverage_description": hd.element(4),
            "coverage_level_code": hd.element(5),  # e.g. IND, FAM
            "coverage_start_dt": dtp_348.element(3),
            "coverage_start_dt_format": dtp_348.element(2),
            "coverage_end_dt": dtp_349.element(3),
            "coverage_end_dt_format": dtp_349.element(2),
            "references": self._ref_list(trimmed),
            "cob_payer_responsible_cd": cob.element(1),
            "cob_policy_number": cob.element(2),
            "cob_indicator_cd": cob.element(3),
            "cob_service_type_cd": cob.element(4),
            "cob_start_dt": dtp_344.element(3),
            "cob_start_dt_format": dtp_344.element(2),
            "cob_end_dt": dtp_345.element(3),
            "cob_end_dt_format": dtp_345.element(2),
        }

    def _health_coverage_elections(self):
        """Slice HD loops; stop before LS/LE reporting or trailing LX reporting blocks."""
        loop = self.member_detail_loop
        hd_positions = [i for i, seg in enumerate(loop) if seg._name == "HD"]
        if not hd_positions:
            return []

        # End boundary: next HD, or first LS/LE after this HD, or first LX that
        # appears after the last HD's immediate children... Use LS/LE/end as hard stops.
        hard_stops = [
            i for i, seg in enumerate(loop) if seg._name in ("LS", "LE")
        ]
        elections = []
        for i, start in enumerate(hd_positions):
            next_hd = hd_positions[i + 1] if i + 1 < len(hd_positions) else len(loop)
            stop_candidates = [next_hd] + [s for s in hard_stops if s > start]
            end = min(stop_candidates)
            elections.append(self.build_plan_elections(loop[start:end]))
        return elections

    def _related_parties(self, member_nm1):
        """NM1(+optional DMG) entities other than the primary member."""
        parties = []
        loop = self.member_detail_loop
        first_hd = next((j for j, s in enumerate(loop) if s._name == "HD"), len(loop))

        i = 0
        while i < len(loop):
            seg = loop[i]
            if seg._name != "NM1":
                i += 1
                continue

            is_member = (
                seg is member_nm1
                or (
                    seg.element(1) == member_nm1.element(1)
                    and seg.element(9) == member_nm1.element(9)
                    and seg.element(3) == member_nm1.element(3)
                )
            )
            # Keep pre-HD related names (70/QD/…) and post-HD Y2 (MCO) entities
            keep = (not is_member) and (i < first_hd or seg.element(1) == "Y2")
            if keep:
                dmg = loop[i + 1] if i + 1 < len(loop) and loop[i + 1]._name == "DMG" else None
                parties.append(self._parse_nm1_person(seg, dmg))
            i += 1
        return parties

    def _member_reporting_categories(self):
        """
        Parse LS/LE*2700 style reporting categories common in Medicaid 834s:
        LX*n ~ N1*75*NAME ~ REF*.. ~ DTP*..
        """
        loop = self.member_detail_loop
        try:
            ls_idx = next(
                i
                for i, s in enumerate(loop)
                if s._name == "LS" and s.element(1) == "2700"
            )
        except StopIteration:
            return []

        try:
            le_idx = next(
                i
                for i, s in enumerate(loop)
                if i > ls_idx and s._name == "LE" and s.element(1) == "2700"
            )
        except StopIteration:
            le_idx = len(loop)

        block = loop[ls_idx + 1 : le_idx]
        categories = []
        current = None
        for seg in block:
            if seg._name == "LX":
                if current:
                    categories.append(current)
                current = {
                    "assigned_number": seg.element(1),
                    "name": "",
                    "references": [],
                    "dates": [],
                }
            elif seg._name == "N1" and current is not None:
                current["name"] = seg.element(2)
                current["entity_identifier_code"] = seg.element(1)
            elif seg._name == "REF" and current is not None:
                qual = seg.element(1)
                current["references"].append(
                    {
                        "ref_qualifier": qual,
                        "ref_qualifier_desc": self.REF_QUALIFIER_MAPPING.get(qual, qual),
                        "ref_value": seg.element(2),
                    }
                )
            elif seg._name == "DTP" and current is not None:
                current["dates"].append(
                    {
                        "dtp_qualifier": seg.element(1),
                        "dtp_format": seg.element(2),
                        "dtp_date": seg.element(3),
                    }
                )
        if current:
            categories.append(current)
        return categories

    def build_enrollment_data(self):
        loop = self.member_detail_loop
        segment_index = self._index_segments(loop)

        ins = self._get_first(segment_index, "INS")
        member_nm1 = self._find_member_nm1(loop)

        # DMG that follows the member NM1 (not a related-party DMG)
        dmg = Segment.empty()
        try:
            nm1_pos = next(i for i, s in enumerate(loop) if s is member_nm1)
            for s in loop[nm1_pos + 1 :]:
                if s._name == "DMG":
                    dmg = s
                    break
                if s._name in ("NM1", "HD", "INS"):
                    break
        except StopIteration:
            dmg = self._get_first(segment_index, "DMG")

        per = self._get_first(segment_index, "PER")
        n3 = self._get_first(segment_index, "N3")
        n4 = self._get_first(segment_index, "N4")

        # Member-level segments only (before first HD)
        first_hd = next((i for i, s in enumerate(loop) if s._name == "HD"), len(loop))
        member_level = loop[:first_hd]

        maint_type = ins.element(3)
        relationship = ins.element(2)
        id_qual = member_nm1.element(8)

        dtp_356 = self._get_dtp(self._index_segments(member_level), "356")
        dtp_303 = self._get_dtp(self._index_segments(member_level), "303")
        # Prefer eligibility begin; fall back to maintenance effective; then any DTP
        coverage_start = (
            dtp_356.element(3)
            or dtp_303.element(3)
            or self._get_first(self._index_segments(member_level), "DTP").element(3)
        )

        return {
            "transaction_header": self.build_transaction_header(),
            "member_id_code": member_nm1.element(9),
            "member_identifier_type": self.IDENTIFIER_TYPE_MAPPING.get(id_qual, id_qual),
            "member_identifier_qualifier": id_qual,
            "member_nm1_entity_code": member_nm1.element(1),
            "member_nm1_entity_desc": self.NM1_ENTITY_MAPPING.get(
                member_nm1.element(1), member_nm1.element(1)
            ),
            "member_first_name": member_nm1.element(4),
            "member_last_name": member_nm1.element(3),
            "member_middle_name": member_nm1.element(5),
            "member_dob": dmg.element(2),
            "member_gender": dmg.element(3),
            "contact_phone_number": self._parse_contact_phones(per),
            "address": {
                "street_name": n3.element(1),
                "apartment_name": n3.element(2),
                "city_name": n4.element(1),
                "state_code": n4.element(2),
                "postal_code": n4.element(3),
                "country_code": n4.element(4),
                "location_qualifier": n4.element(5),
                "location_identifier": n4.element(6),
            },
            "references": self._ref_list(member_level),
            "member_dates": self._dtp_list(member_level, self.MEMBER_DTP_MAPPING),
            "Maintenance": {
                "benefit_status": ins.element(1),
                "individual_relationship_code": relationship,
                "individual_relationship_desc": self.RELATIONSHIP_MAPPING.get(
                    relationship, relationship
                ),
                "maintenance_type_code": maint_type,
                "maintenance_type_desc": self.MAINTENANCE_TYPE_MAPPING.get(
                    maint_type, maint_type
                ),
                "maintenance_reason_code": ins.element(4),
                "benefit_status_code": ins.element(5),
                "medicare_plan_code": ins.element(6),
                "cobra_qualifying_event": ins.element(7),
                "employment_status_code": ins.element(8),
                "coverage_start_date": coverage_start,
                "maintenance_effective_date": dtp_303.element(3),
                "eligibility_begin_date": dtp_356.element(3),
            },
            "related_parties": self._related_parties(member_nm1),
            "health_coverage_elections": self._health_coverage_elections(),
            "reporting_categories": self._member_reporting_categories(),
        }

    def to_json(self):
        return {"enrollment_member": self.enrollment_data}
