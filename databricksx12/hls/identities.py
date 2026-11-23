from databricksx12.edi import Segment
from typing import List, Dict
import itertools
from collections import defaultdict
from functools import reduce

class Identity:

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}
    

#
# Hanlde providers across 837i and 837p claims
#  - Billing, servicing, facility, attending, operating, other
#
class ProviderIdentity(Identity):

    def __init__(self, nm1=Segment.empty(), n3=Segment.empty(), n4=Segment.empty(), ref = Segment.empty(), prv = Segment.empty()):
        self.npi = nm1.element(9)
        self.entity_type = 'Organization' if nm1.element(2) == '2' else 'Individual'
        self.name = nm1.element(3) if self.entity_type == 'Organization' else ' '.join([nm1.element(3), nm1.element(4,dne=""), nm1.element(5)])
        self.street = n3.element(1)
        self.city = n4.element(1)
        self.state = n4.element(2)
        self.zip = n4.element(3)
        self.ein_type = ref.element(1)
        self.ein = ref.element(2)
        self.taxonomy = prv.element(3)
        self.provider_role = prv.element(1)
        
class PayerIdentity(Identity):
    def __init__(self, nm1):
        self.payer_name = nm1.element(3)
        self.payer_identifier = nm1.element(9)
        self.payer_identifier_cd = nm1.element(8)
        self.business_entity_type = nm1.element(2)

class PatientIdentity(Identity):
        def __init__(self, nm1, n3, n4, dmg, pat, sbr, ref_ea=Segment.empty(), ref_sy=Segment.empty()):
            self.subsciber_identifier = nm1.element(9)
            self.name = ' '.join([nm1.element(3), nm1.element(4), nm1.element(5)])
            self.patient_relationship_cd = pat.element(1)
            self.payer_responsibility_sequence_number_code = sbr.element(1)
            self.insurance_group_number = sbr.element(3)
            self.insurance_plan_name = sbr.element(4)
            self.insurance_type_cd = sbr.element(5)
            self.subscriber_relationship_cd = sbr.element(2)
            self.claim_filing_indicator_code = sbr.element(9)
            self.street = n3.element(1)
            self.city = n4.element(1)
            self.state = n4.element(2)
            self.zip = n4.element(3)
            self.dob = dmg.element(2)
            self.dob_format = dmg.element(1)
            self.gender_cd = dmg.element(3)
            self.mrn = ref_ea.element(2)
            self.ssn = ref_sy.element(2)

class ClaimIdentity(Identity):
    #
    # clm, cl1 are individual segments
    # dtp is a loop of 0 or more dates 
    #
    def __init__(self, clm, dtp, cl1 = Segment.empty(), k3 = Segment.empty(), hi = Segment.empty(), ref = [], amt = [],  principal_hi = Segment.empty(), other_hi = [], condition_hi = [],
                 visit_reason_hi = [], bht = Segment.empty(), other_payer_groups = []):
        self.claim_id = clm.element(1)
        self.claim_amount = clm.element(2)
        self.facility_type_code = clm.element(5)
        # CLM05 composite: facility / place-of-service code, qualifier (A = UB bill type,
        # B = place of service), claim frequency (1 original, 7 replacement, 8 void, ...)
        self.facility_code = clm.element(5, 0)
        self.facility_code_qualifier = clm.element(5, 1)
        self.claim_frequency_cd = clm.element(5, 2)
        self.provider_signature_ind = clm.element(6)
        self.provider_accept_assignment_cd = clm.element(7)
        self.benefits_assignment_ind = clm.element(8)
        self.release_of_info_cd = clm.element(9)
        self.delay_reason_cd = clm.element(20)
        self.claim_dates = [{'date_cd': s.element(1), 'date_format': s.element(2), 'date': s.element(3)} for s in dtp]
        self.admission_type = cl1.element(1)
        self.admission_src_cd = cl1.element(2)
        self.discharge_status_cd = cl1.element(3)
        self.encounter_id = k3.element(1)
        self.drg_cd = hi.element(1)
        self.clm_refs = [{'id_code_qualifier': x.element(1), 'id': x.element(2)} for x in ref]
        self.other_amts = [{'amt_qualifier_cd': a.element(1), 'amt': a.element(2)} for a in amt]
        self.icd10_pcs_cds = { #only 837i
            'principal_prcdr': {'prcdr_cd': principal_hi.element(1,1), 'date_format': principal_hi.element(1,2), 'date': principal_hi.element(1,3)},
            'other_cds': list(itertools.chain(*[
                [{'prcdr_cd': s.element(i,1), 'date_format': s.element(i,2), 'date': s.element(i,3)} for i in list(range(1, s.segment_len()))]
            for s in other_hi])) 
        }
        # Condition codes from HI*BG segments (837I only)
        self.condition_codes = list(itertools.chain(*[
            [s.element(i, 1) for i in range(1, s.segment_len()) if s.element(i, 0) == "BG"]
            for s in condition_hi
        ]))
        # Patient reason for visit, HI*APR (837I only)
        self.visit_reason_codes = list(itertools.chain(*[
            [s.element(i, 1) for i in range(1, s.segment_len()) if s.element(i, 0) == "APR"]
            for s in visit_reason_hi
        ]))
        # Transaction-level, from BHT (beginning of hierarchical transaction).
        # Shared by every claim in the same ST..SE.
        self.transaction_created_date = bht.element(4)
        self.transaction_created_time = bht.element(5)
        self.transaction_purpose_cd   = bht.element(2)
        self.transaction_ref_id       = bht.element(3)
        self.claim_or_encounter_cd    = bht.element(6)
        # Loop 2320/2330: other payers on this claim (coordination of benefits) --
        # one entry per SBR in the claim loop, with what that payer paid and adjusted.
        self.other_payers = [OtherPayerIdentity(g).to_dict() for g in other_payer_groups]

# POA is the last sub element of the respective segments
class DiagnosisIdentity(Identity):
    def __init__(self, hi_segments):
        self.principal_dx_cd = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABK"] == [] else [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABK"][0]
        self.principal_dx_poa = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABK"] == [] else [s.element(1,s.sub_element_len(1)-1) for s in hi_segments if s.element(1,0) == "ABK"][0]
        self.admitting_dx_cd = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABJ"] == []	else [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABJ"][0]
        self.admitting_dx_poa = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABJ"] == []	else [s.element(1,s.sub_element_len(1)-1) for s in hi_segments if s.element(1,0) == "ABJ"][0]
        self.reason_visit_dx_cd = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "APR"] == [] else [s.element(1,1) for s in hi_segments if s.element(1, 0) == "APR"][0]
        self.reason_visit_dx_poa = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "APR"] == [] else [s.element(1,s.sub_element_len(1)-1) for s in hi_segments if s.element(1,0) == "APR"][0]
        self.external_injury_dx_cd = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABN"] == [] else [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABN"][0]
        self.external_injury_dx_poa = "" if [s.element(1,1) for s in hi_segments if s.element(1, 0) == "ABN"] == [] else [s.element(1,s.sub_element_len(1)-1) for s in hi_segments if s.element(1,0) == "ABN"][0]
        self.other_dx_cds = list(itertools.chain(*[[{'dx_cd': s.element(i,1), 'poa': s.element(i,s.sub_element_len(i)-1)} for i in list(range(1, s.segment_len())) if s.element(i,0) == "ABF"]
            for s in hi_segments]))
    
class Submitter_Receiver_Identity(Identity):
    def __init__(self, nm1=Segment.empty(), per= Segment.empty()):
        self.type = 'Organization' if nm1.element(2) == '2' else 'Individual'
        self.name = nm1.element(3) if self.type == 'Organization' else ' '.join([nm1.element(3), nm1.element(4,dne=""), nm1.element(5)])
        if per.element(0):
            self.sbmtter_contact_name = "" if per.element(2) == '' else per.element(2)
            self.sbmtter_contacts = "" if (per.element(3), per.element(4)) == [] else [(per.element(3), per.element(4)), #telephone
                                                                                    (per.element(5), per.element(6))] # usually fax/email

                
def _cas_adjustments(cas_segments):
    """CAS01 group, then (reason, amount, quantity) triplets in CAS02-04, CAS05-07, ..."""
    return [{'adjustment_grp_cd': c.element(1), 'adjustment_reason_cd': c.element(i),
             'adjustment_amount': c.element(i + 1), 'adjustment_quantity': c.element(i + 2)}
            for c in cas_segments for i in range(2, c.segment_len(), 3) if c.element(i) != ""]


#
# Loop 2320 (other subscriber information) + 2330B (other payer) on an 837 claim.
# @param segments - the claim-loop segments from one SBR up to the next SBR (or the end
#                   of the claim loop)
#
class OtherPayerIdentity(Identity):
    def __init__(self, segments):
        first = lambda name, q=None: next((s for s in segments if s._name == name and (q is None or s.element(1) == q)), Segment.empty())
        sbr = first("SBR")
        pr = first("NM1", "PR")
        amt = lambda q: first("AMT", q).element(2)
        pr_idx = next((i for i, s in enumerate(segments) if s._name == "NM1" and s.element(1) == "PR"), len(segments))
        self.payer_responsibility_sequence_number_code = sbr.element(1)
        self.subscriber_relationship_cd = sbr.element(2)
        self.insurance_group_number = sbr.element(3)
        self.insurance_plan_name = sbr.element(4)
        self.insurance_type_cd = sbr.element(5)
        self.claim_filing_indicator_code = sbr.element(9)
        self.payer_name = pr.element(3)
        self.payer_identifier_cd = pr.element(8)
        self.payer_identifier = pr.element(9)
        self.payer_paid_amt = amt("D")
        self.noncovered_amt = amt("A8")
        self.remaining_patient_liability_amt = amt("EAF")
        self.adjudication_date = next((s.element(3) for s in segments if s._name == "DTP" and s.element(1) == "573"), "")
        # REF after NM1*PR (2330B): F8 = the other payer's claim control number
        self.payer_refs = [{'id_code_qualifier': s.element(1), 'id': s.element(2)} for s in segments[pr_idx:] if s._name == "REF"]
        self.claim_adjustments = _cas_adjustments([s for s in segments if s._name == "CAS"])


class ServiceLine(Identity):

    def __init__(self, d):
        for k,v in d.items():
            setattr(self,k,v)

    @staticmethod
    def _adjudications(seg_list):
        """Loop 2430: one entry per SVD (a prior payer's line adjudication)."""
        idx = [i for i, s in enumerate(seg_list) if s._name == "SVD"] + [len(seg_list)]
        out = []
        for a, b in zip(idx, idx[1:]):
            grp = seg_list[a:b]
            svd = grp[0]
            out.append({
                'other_payer_identifier': svd.element(1),
                'paid_amt': svd.element(2),
                'prcdr_cd_type': svd.element(3, 0),
                'prcdr_cd': svd.element(3, 1),
                'revenue_cd': svd.element(4),
                'paid_units': svd.element(5),
                'adjustments': _cas_adjustments([s for s in grp if s._name == "CAS"]),
                'adjudication_date': next((s.element(3) for s in grp if s._name == "DTP" and s.element(1) == "573"), "")
            })
        return out

    @staticmethod
    def common(sv, lx, dtp, amt, lin = [], ref = [], line = []):
        return {
            "claim_line_number": lx.element(1),
            "service_dates": [{'date_cd': s.element(1), 'date_format': s.element(2), 'date': s.element(3)} for s in dtp],
            'other_amts': [{'amt_qualifier_cd': a.element(1), 'amt': a.element(2)} for a in amt],
            'line_item_identifiers': [{'line_item_code_qualifier': l.element(2), 'line_item_value': l.element(3)} for l in lin], #uncommon in 837s but do occur
            # REF*6R = line item control number, echoed on the 835 to tie a paid line back
            'line_refs': [{'id_code_qualifier': x.element(1), 'id': x.element(2)} for x in ref],
            'line_adjudications': ServiceLine._adjudications(line)
        }

    #
    # Institutional Claims
    #
    @classmethod
    def from_sv2(cls, sv2, lx, dtp, amt, lin, ref = [], line = []):
        return cls({**cls.common(sv2, lx, dtp,amt, lin, ref, line),
                    **{
                        "units": sv2.element(5),
                        "units_measurement": sv2.element(4),
                        "line_chrg_amt": sv2.element(3),
                        "prcdr_cd": sv2.element(2, 1, ""),
                        "prcdr_cd_type": sv2.element(2, 0, ""),
                        "modifier_cds": ','.join(filter(lambda x: x!="", [sv2.element(2, 2, ""), sv2.element(2, 3, ""), sv2.element(2, 4,""), sv2.element(2, 5, "")])),
                        "revenue_cd": sv2.element(1),
                        "service_dates": [{'date_cd': s.element(1), 'date_format': s.element(2), 'date': s.element(3)} for s in dtp]
                    }
                })

    #
    # Professional Claims
    #
    @classmethod
    def from_sv1(cls, sv1, lx, dtp, amt, lin, ref = [], line = []):
        return cls({**cls.common(sv1, lx, dtp, amt, lin, ref, line),
                    **{
                        "units": sv1.element(4),
                        "units_measurement": sv1.element(3),
                        "line_chrg_amt": sv1.element(2),
                        "prcdr_cd": sv1.element(1, 1),
                        "prcdr_cd_type": sv1.element(1, 0),
                        "modifier_cds": ','.join(filter(lambda x: x!="", [sv1.element(1, 2, ""), sv1.element(1, 3, ""), sv1.element(1, 4,""), sv1.element(1, 5, "")])),
                        "place_of_service": sv1.element(5),
                        "dg_cd_pntr": sv1.element(7),
                        "service_dates": [{'date_cd': s.element(1), 'date_format': s.element(2), 'date': s.element(3)} for s in dtp]
                    }
                })
        

        
