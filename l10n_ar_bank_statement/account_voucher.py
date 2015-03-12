# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2010-2014 Eynes - Ingeniería del software All Rights Reserved
#    Copyright (c) 2014 Aconcagua Team (http://www.proyectoaconcagua.com.ar)
#    All Rights Reserved. See AUTHORS for details.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from osv import osv, fields
from tools.translate import _
from datetime import datetime, date, time, timedelta

class account_voucher(osv.osv):
    _inherit = 'account.voucher'
    
    def proforma_voucher(self, cr, uid, ids, context=None):
        vouchers = super(account_voucher, self).proforma_voucher(cr, uid, ids, context=context)
        print 'vouchers = super(account_voucher, self).proforma_voucher(cr, uid, ids, context=context)'
        stl = []
        
        for vou in self.browse(cr, uid, ids, context=context):
            if vou.type in 'receipt':
                sign = 1
                aux_account = vou.partner_id.property_account_receivable.id
            if vou.type in 'payment':
                sign = -1
                
                aux_account = vou.partner_id.property_account_payable.id
            
            for line in vou.payment_line_ids:
                if line.payment_mode_id.journal_id and line.payment_mode_id.journal_id.type in 'bank':
                    aux_name = line.voucher_id.number
                    
                    if sign:
                        amount = line.amount * sign
                    else:
                        amount = line.amount * sign
                    
                    if line.date:
                        aux_date = line.date
                    else:
                        aux_date = vou.date
                        
                    st_line = {
                        'name': line.payment_mode_id.name,
                        'date': vou.date,
                        'payment_date': aux_date,
                        'amount': amount,
                        'account_id': aux_account,
                        'state': 'draft',
                        'type': vou.type,
                        'bank_statement': True,
                        'partner_id': line.voucher_id.partner_id and line.voucher_id.partner_id.id,
                        'ref_voucher_id': vou.id,
                        'creation_type': 'system',
                        #~ 'ref': vou.reference,
                        'ref': vou.number,
                        'journal_id': line.payment_mode_id.journal_id.id,
                    }

                    st_id = self.pool.get('account.bank.statement.line').create(cr, uid, st_line, context)
                
            for issued_check in vou.issued_check_ids:
                if issued_check.type in 'common':
                    aux_payment_date = issued_check.issue_date
                else:
                    aux_payment_date = issued_check.payment_date
                    
                st_line = {
                    'name': 'Cheque nro ' + issued_check.number,
                    'issue_date': issued_check.issue_date,
                    'payment_date': aux_payment_date,
                    'amount': issued_check.amount*-1,
                    'account_id': aux_account,
                    'ref': vou.number,
                    'state': 'draft',
                    'type': 'payment',
                    'bank_statement': True,
                    'partner_id': vou.partner_id and vou.partner_id.id,
                    'ref_voucher_id': vou.id,
                    'creation_type': 'system',
                    'journal_id': issued_check.account_bank_id.journal_id.id,
                }

                st_id = self.pool.get('account.bank.statement.line').create(cr, uid, st_line, context)

            return True
        
    def cancel_voucher(self, cr, uid, ids, vals, context=None):
        
        statement_line_obj = self.pool.get('account.bank.statement.line')
        
        for voucher in self.browse(cr, uid, ids, context=None):
            for statement_line in voucher.statement_bank_line_ids:
                sql = 'delete from account_bank_statement_line where id = ' + str(statement_line.id)
                cr.execute(sql)
        return super(account_voucher, self).cancel_voucher(cr, uid, ids, vals, context=None)

account_voucher()
