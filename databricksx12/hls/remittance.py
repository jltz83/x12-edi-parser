from databricksx12.hls.claim import MedicalClaim
import functools
#
# 835 payment information
#  https://datainsight.health/edi/payments/with-discount/
#
class Remittance(MedicalClaim):

    NAME = "835"
    def __init__(self,
                 trx_header_loop,
                 payer_loop,
                 payee_loop,
                 clm_loop,
                 trx_summary_loop,
                 header_number_loop):
        self.trx_header_loop = trx_header_loop
        self.payer_loop = payer_loop
        self.payee_loop = payee_loop
        self.clm_loop = clm_loop
        self.trx_summary_loop = trx_summary_loop
        self.header_number_loop = header_number_loop 
        self.build()

    def build(self):
        self.trx_header_info = self.populate_trx_loop()
        self.payer_info = self.populate_payer_loop()
        self.payee_info = self.populate_payee_loop()
        self.clm_info = self.populate_claim_loop()
        self.plb_info = self.populate_plb_loop()
        self.header_info = self.populate_header_loop()
        self._loop_segments_info = None   # built on first access

    @property
    def loop_segments_info(self):
        if getattr(self, '_loop_segments_info', None) is None:
            self._loop_segments_info = {
                'trx_header_loop': self._extract_segments(self.trx_header_loop),
                'payer_loop': self._extract_segments(self.payer_loop),
                'payee_loop': self._extract_segments(self.payee_loop),
                'clm_loop': self._extract_segments(self.clm_loop ),
                'trx_summary_loop': self._extract_segments(self.trx_summary_loop ),
                'header_number_loop': self._extract_segments(self.header_number_loop)
            }
        return self._loop_segments_info

    def populate_header_loop(self): 
        return {
            'provider_id': self._first(self.header_number_loop, 'TS3').element(1)
        }
    #
    # Assuming npi, date, then repeating <reason cd:id, adj amount>
    #
    def populate_plb_loop(self):
        return functools.reduce(lambda x, y: x+y, [
            [{
                'provider_adjustment_npi': p.element(1),
                'provider_adjustment_date': p.element(2),
                'provider_adjustment_reason_cd': p.element(i, 0),
                'provider_adjustment_id': p.element(i, 1),
                'provider_adjustment_amt': p.element(i+1)
            }
             for i in list(range(3,p.segment_len(), 2))]
            for p in self.segments_by_name("PLB", data=self.trx_summary_loop)], [])

    def populate_payer_loop(self):
        return {
            'entity_id_cd': self._first(self.payer_loop, "N1").element(1),
            'payer_name': self._first(self.payer_loop, "N1").element(2),
            'payer_street': self._first(self.payer_loop, "N3").element(1),
            'payer_city': self._first(self.payer_loop, "N4").element(1),
            'payer_state': self._first(self.payer_loop, "N4").element(2),
            'payer_zip': self._first(self.payer_loop, "N4").element(3),
            'payer_contact_info': [
                {
                    'payer_contact_name': c.element(2),
                    'payer_contact_function_cd': c.element(1),
                    'payer_contact_number': c.element(6),
                    'payer_email': c.element(4)
                }
                for c in self.segments_by_name("PER", data = self.payer_loop)],
            'payer_primary_id': self._first(self.payer_loop, "REF").element(1),
            'payer_secondary_id': self._first(self.payer_loop, "REF").element(2),
            # every payer-loop REF (2U payer ID, EO submitter ID, NF NAIC code, ...)
            'payer_refs': [{'id_code_qualifier': x.element(1), 'id': x.element(2)}
                           for x in self.segments_by_name("REF", data=self.payer_loop)]
        }

    #
    # N1*PE: N101 entity (PE), N102 name, N103 id qualifier (XX = NPI, FI = tax ID), N104 id.
    # 'payee_npi' / 'payee_id_cd' keep their historical names (they hold N103 / N104);
    # 'payee_id_qualifier' and 'payee_id' are the unambiguous versions.
    #
    def populate_payee_loop(self):
        n1 = self._first(self.payee_loop, "N1")
        refs = self.segments_by_name("REF", data=self.payee_loop)
        tj = [x for x in refs if x.element(1) == "TJ"]
        return {
            'payee_name': n1.element(2),
            'payee_npi': n1.element(3),
            'payee_id_cd': n1.element(4),
            'payee_id_qualifier': n1.element(3),
            'payee_id': n1.element(4),
            'payee_tax_id': (tj[0].element(2) if tj else self._first(self.payee_loop, "REF").element(2)),
            'payee_refs': [{'id_code_qualifier': x.element(1), 'id': x.element(2)} for x in refs]
        }
    
    def populate_trx_loop(self):
        return {
            'transaction_handling_cd': self._first(self.trx_header_loop,"BPR").element(1),
            'monetary_amt': self._first(self.trx_header_loop,"BPR").element(2),
            'credit_debit_flag': self._first(self.trx_header_loop,"BPR").element(3),
            'payment_method_cd': self._first(self.trx_header_loop,"BPR").element(4),
            'payment_date': self._first(self.trx_header_loop,"BPR").element(16),
            'trace_type_cd': self._first(self.trx_header_loop,"TRN").element(1),
            'trace_reference_id': self._first(self.trx_header_loop,"TRN").element(2),            
            'trace_origin_company_id':  self._first(self.trx_header_loop,"TRN").element(3),
            # DTM*405 production date (the date the payer produced the remittance)
            'production_date': self._first([x for x in self.trx_header_loop if x._name == "DTM" and x.element(1) == "405"], "DTM").element(2)
            }

    def _svc_bounds(self):
        """(index, segment, end) for every SVC in the claim loop. A line runs to the
        next SVC, or to the END of the claim loop for the last line.

        The previous form, min(index_of_segment(...'SVC', i+1), len(clm_loop)-1),
        returned -1 for the last line when no SVC followed it, and clm_loop[idx:-1]
        silently dropped the claim's final segment -- often the last line's CAS,
        LQ remark or REF*6R line control number."""
        svcs = self.segments_by_name_index(segment_name="SVC", data=self.clm_loop)
        ends = [i for i, _ in svcs[1:]] + [len(self.clm_loop)]
        return [(i, seg, end) for (i, seg), end in zip(svcs, ends)]

    def populate_claim_loop(self):
        end_clp_index = ([i for i,z in enumerate([y._name for y in self.clm_loop[1:]]) if z == "CLP"] + [len(self.clm_loop)])[0]
        svc_bounds = self._svc_bounds()
        # claim-level portion of the 2100 loop: CLP up to the first SVC (or the whole loop)
        header_end = svc_bounds[0][0] if svc_bounds else len(self.clm_loop)
        header = self.clm_loop[:header_end]
        clp = self._first(self.clm_loop, "CLP")
        mia = self._first(header, "MIA")
        moa = self._first(header, "MOA")
        return {
            'claim_id': clp.element(1),
            'person_or_organization': self._populate_names(self.clm_loop[:end_clp_index]),
            'claim_status_cd': clp.element(2),
            'claim_chrg_amt': clp.element(3),
            'claim_pay_amt': clp.element(4),
            'patient_pay_amt': clp.element(5),
            'claim_filing_cd': clp.element(6),
            'payer_claim_id': clp.element(7),
            'type_of_bill_cd': clp.element(8),
            'claim_freq_cd': clp.element(9),
            'drg_cd': clp.element(11),
            'drg_weight': clp.element(12),
            'discharge_fraction': clp.element(13),
            'patient_entity_id_cd': self._first(self.clm_loop,"NM1").element(1),
            'entity_type_qualifier': self._first(self.clm_loop,"NM1").element(2),
            # NM103 = last name, NM104 = first name (previously read one position late)
            'patient_last_nm': self._first(self.clm_loop,"NM1").element(3),
            'patient_first_nm': self._first(self.clm_loop,"NM1").element(4),
            'id_code_qualifier': self._first(self.clm_loop,"NM1").element(8),
            'patient_id': self._first(self.clm_loop,"NM1").element(9),
            'clm_refs': [{'id_code_qualifier': x.element(1), 'id': x.element(2)}  for x in self.segments_by_name("REF", data=header)],
            #Claim level service adjustments CAS (before the first SVC; the whole loop when there are no lines)
            'service_adjustments': functools.reduce(lambda x,y: x+y,
                [self.populate_adjustment_groups(x) for x in self.segments_by_name("CAS", data = header[1:])], []),
            # Claim-level remark codes: MOA03-07 (outpatient), MIA05 and MIA20-23 (inpatient)
            'claim_remarks': [c for c in ([moa.element(i) for i in range(3, 8)] +
                                          [mia.element(5)] + [mia.element(i) for i in range(20, 24)]) if c],
            'inpatient_adjudication': ({'covered_days': mia.element(1), 'pps_operating_outlier_amt': mia.element(2),
                                        'lifetime_psychiatric_days': mia.element(3), 'drg_amt': mia.element(4),
                                        'cost_report_day_count': mia.element(15)} if mia.data else None),
            'outpatient_adjudication': ({'reimbursement_rate': moa.element(1), 'hcpcs_payable_amt': moa.element(2),
                                         'esrd_payment_amt': moa.element(8), 'nonpayable_professional_amt': moa.element(9)}
                                        if moa.data else None),
            'claim_amts': [{'amt_qualifier_cd': a.element(1), 'amt': a.element(2)} for a in self.segments_by_name("AMT", data=header)],
            'claim_qtys': [{'qty_qualifier_cd': q.element(1), 'qty': q.element(2)} for q in self.segments_by_name("QTY", data=header)],
            'claim_lines': [self.populate_claim_line(seg, i, end) for i, seg, end in svc_bounds],
            'date_references': [{'date_cd': x.element(1), 'date': x.element(2)} for x in header if x._name == "DTM"]
        }

    def _populate_names(self, loop):
        return [
            {
                "entity_id_cd": x.element(1),
                "entity_type_qualifier": x.element(2),
                "entity_last_or_organization_name": x.element(3),
                "entity_first": x.element(4),
                "id_cd_qualifier": x.element(8),
                "id_cd": x.element(9)
            }
                 for x in loop if x._name== "NM1"]

    
    #
    # @parma svc - the svc segment for the service rendered
    # @param idx - the index where the svc is found within self.clm_loop
    # @param svc_end_idx - the first segment AFTER the service line (exclusive bound)
    #
    # SVC01 / SVC06 are composites (qualifier, code, modifiers 1-4, description) split on
    # the file's own component separator (ISA16). 'prcdr_cd' keeps the raw composite for
    # backward compatibility; files whose ISA16 is not ':' (e.g. '>') could not be split
    # downstream on ':' and lost the procedure code.
    #
    def populate_claim_line(self, svc, idx, svc_end_idx):
        line = self.clm_loop[idx:svc_end_idx]
        dtms = [x for x in line if x._name == "DTM"]
        return {
            'prcdr_cd':svc.element(1),
            'prcdr_qualifier': svc.element(1, 0),
            'prcdr_code': svc.element(1, 1),
            'modifier_cds': ','.join(m for m in [svc.element(1, k) for k in range(2, 6)] if m),
            'prcdr_description': svc.element(1, 6),
            'chrg_amt':svc.element(2),
            'paid_amt':svc.element(3),
            'rev_cd':svc.element(4),
            'units': svc.element(5),
            'original_prcdr_cd':svc.element(6),
            'original_prcdr_qualifier': svc.element(6, 0),
            'original_prcdr_code': svc.element(6, 1),
            'original_units': svc.element(7),
            # dates belonging to THIS line only (previously the first DTM at or after the
            # line, which was the next line's date when a line carried none)
            'service_date_qualifier_cd': (dtms[0].element(1) if dtms else ""),
            'service_date': (dtms[0].element(2) if dtms else ""),
            'service_dates': [{'date_cd': x.element(1), 'date': x.element(2)} for x in dtms],
            'other_amts': [{'amt_qualifier_cd': a.element(1), 'amt': a.element(2)} for a in self.segments_by_name("AMT", data=line) ],
            'remarks': [{'qualifier_cd': x.element(1), 'remark_cd': x.element(2)} for x in self.segments_by_name("LQ", data = line)],
            #line level service adjustments
            'service_adjustments': functools.reduce(lambda x,y: x+y,
                [self.populate_adjustment_groups(x) for x in self.segments_by_name("CAS", data = line)], []),
            'line_refs': [{'id_code_qualifier': x.element(1), 'id': x.element(2)} for x in  self.segments_by_name("REF", data=line)]
        }

    #
    # CAS: CAS01 group code, then up to six (reason, amount, quantity) triplets in
    # CAS02-04, CAS05-07, ... CAS17-19. The group code applies to every triplet.
    #
    # The previous form read the "group" of the 2nd+ triplets from the position of the
    # PREVIOUS triplet's quantity (CAS04, CAS07, ...), so a CAS with a quantity produced
    # adjustments with group codes like "1" -- and a CO-24 or CO-45 in second position
    # could not be found by group + reason.
    #
    def populate_adjustment_groups(self, cas):
        grp = cas.element(1)
        return [{'adjustment_grp_cd': grp,
                 'adjustment_reason_cd': cas.element(i),
                 'adjustment_amount': cas.element(i+1),
                 'adjustment_quantity': cas.element(i+2)}
                for i in range(2, cas.segment_len(), 3) if cas.element(i) != ""]

    def to_json(self, include_input_loops=True):
        return {
            **{'payment': self.trx_header_info},
            **{'payer': self.payer_info},
            **{'payee': self.payee_info},
            **{'claim': self.clm_info},
            **{'provider_adjustments': self.plb_info},
            **{'header_info': self.header_info},
            **({'input_loop_segments': self.loop_segments_info} if include_input_loops else {}) #the input loops

        }
