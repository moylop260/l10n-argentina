# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (c) 2013 E-MIPS (http://www.e-mips.com.ar) All Rights Reserved.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from osv import osv, fields
from datetime import datetime
from tools.translate import _
import pooler
import time
import re

__author__ = "Sebastian Kennedy <skennedy@e-mips.com.ar>"

class account_invoice(osv.osv):
    _name = "account.invoice"
    _inherit = "account.invoice"

    _columns = {
        'aut_cae': fields.boolean('Autorizar', help='Pedido de autorizacion a la AFIP'),
        'cae': fields.char('CAE/CAI', size=32, required=False, help='CAE (Codigo de Autorizacion Electronico assigned by AFIP.)'),
        'cae_due_date': fields.date('CAE Due Date', required=False, help='Fecha de vencimiento del CAE'),
        #'associated_inv_ids': fields.many2many('account.invoice', )
        # Campos para facturas de exportacion. Aca ninguno es requerido,
        # eso lo hacemos en la vista ya que depende de si es o no factura de exportacion
        'export_type_id': fields.many2one('wsfex.export_type.codes', 'Export Type'),
        'dst_country_id': fields.many2one('wsfex.dst_country.codes', 'Dest Country'),
        'dst_cuit_id': fields.many2one('wsfex.dst_cuit.codes', 'Country CUIT'),
        'shipping_perm_ids': fields.one2many('wsfex.shipping.permission', 'invoice_id', 'Shipping Permissions'),
        'incoterm_id': fields.many2one('stock.incoterms', 'Incoterm', help="International Commercial Terms are a series of predefined commercial terms used in international transactions."),
        'wsfex_request_ids': fields.one2many('wsfex.request.detail', 'invoice_id'),
    }

    _defaults = {
        'aut_cae': lambda *a: False,
    }

    def onchange_partner_id(self, cr, uid, ids, type, partner_id,\
            date_invoice=False, payment_term=False, partner_bank_id=False, company_id=False , context=None):
        res =   super(account_invoice, self).onchange_partner_id(cr, uid, ids, type, partner_id,\
                date_invoice=False, payment_term=False, partner_bank_id=False, company_id=False)

        if partner_id:
            partner = self.pool.get('res.partner').browse(cr, uid, partner_id, context=context)
            country_id = partner.country_id.id or False
            if country_id:
                dst_country = self.pool.get('wsfex.dst_country.codes').search(cr, uid, [('country_id','=',country_id)])

                if dst_country:
                    res['value'].update({'dst_country_id': dst_country[0]})
        return res




    # Esto lo hacemos porque al hacer una nota de credito, no le setea la fiscal_position
    # Ademas, seteamos el comprobante asociado
    def refund(self, cr, uid, ids, date=None, period_id=None, description=None, journal_id=None, context=None):
        new_ids = super(account_invoice, self).refund(cr, uid, ids, date, period_id, description, journal_id, context=context)
        for id in new_ids:
            inv = self.browse(cr, uid, id)
            if not inv.fiscal_position:
                fiscal_position = inv.partner_id.property_account_position
                vals = {'fiscal_position':fiscal_position.id}

                # TODO: Agregamos el comprobante asociado. Falta terminar el codigo para hacer lo de comprobantes asociados
                self.write(cr, uid, id, vals)
        return new_ids


    # Heredamos esta funcion para quitarle el post de los asientos contables
    # asi luego los podemos cancelar en caso que sea necesario
    def action_move_create(self, cr, uid, ids, context=None):
        """Creates invoice related analytics and financial move lines"""
        ait_obj = self.pool.get('account.invoice.tax')
        cur_obj = self.pool.get('res.currency')
        period_obj = self.pool.get('account.period')
        payment_term_obj = self.pool.get('account.payment.term')
        journal_obj = self.pool.get('account.journal')
        move_obj = self.pool.get('account.move')
        if context is None:
            context = {}
        for inv in self.browse(cr, uid, ids, context=context):
            if not inv.journal_id.sequence_id:
                raise osv.except_osv(_('Error!'), _('Please define sequence on the journal related to this invoice.'))
            if not inv.invoice_line:
                raise osv.except_osv(_('No Invoice Lines!'), _('Please create some invoice lines.'))
            if inv.move_id:
                continue

            ctx = context.copy()
            ctx.update({'lang': inv.partner_id.lang})
            if not inv.date_invoice:
                self.write(cr, uid, [inv.id], {'date_invoice': fields.date.context_today(self,cr,uid,context=context)}, context=ctx)
            company_currency = self.pool['res.company'].browse(cr, uid, inv.company_id.id).currency_id.id
            # create the analytical lines
            # one move line per invoice line
            iml = self._get_analytic_lines(cr, uid, inv.id, context=ctx)
            # check if taxes are all computed
            compute_taxes = ait_obj.compute(cr, uid, inv.id, context=ctx)
            self.check_tax_lines(cr, uid, inv, compute_taxes, ait_obj)

            # I disabled the check_total feature
            group_check_total_id = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'account', 'group_supplier_inv_check_total')[1]
            group_check_total = self.pool.get('res.groups').browse(cr, uid, group_check_total_id, context=context)
            if group_check_total and uid in [x.id for x in group_check_total.users]:
                if (inv.type in ('in_invoice', 'in_refund') and abs(inv.check_total - inv.amount_total) >= (inv.currency_id.rounding/2.0)):
                    raise osv.except_osv(_('Bad Total!'), _('Please verify the price of the invoice!\nThe encoded total does not match the computed total.'))

            if inv.payment_term:
                total_fixed = total_percent = 0
                for line in inv.payment_term.line_ids:
                    if line.value == 'fixed':
                        total_fixed += line.value_amount
                    if line.value == 'procent':
                        total_percent += line.value_amount
                total_fixed = (total_fixed * 100) / (inv.amount_total or 1.0)
                if (total_fixed + total_percent) > 100:
                    raise osv.except_osv(_('Error!'), _("Cannot create the invoice.\nThe related payment term is probably misconfigured as it gives a computed amount greater than the total invoiced amount. In order to avoid rounding issues, the latest line of your payment term must be of type 'balance'."))

            # one move line per tax line
            iml += ait_obj.move_line_get(cr, uid, inv.id)

            entry_type = ''
            if inv.type in ('in_invoice', 'in_refund'):
                ref = inv.reference
                entry_type = 'journal_pur_voucher'
                if inv.type == 'in_refund':
                    entry_type = 'cont_voucher'
            else:
                ref = self._convert_ref(cr, uid, inv.number)
                entry_type = 'journal_sale_vou'
                if inv.type == 'out_refund':
                    entry_type = 'cont_voucher'

            diff_currency_p = inv.currency_id.id <> company_currency
            # create one move line for the total and possibly adjust the other lines amount
            total = 0
            total_currency = 0
            total, total_currency, iml = self.compute_invoice_totals(cr, uid, inv, company_currency, ref, iml, context=ctx)
            acc_id = inv.account_id.id

            name = inv['name'] or inv['supplier_invoice_number'] or '/'
            totlines = False
            if inv.payment_term:
                totlines = payment_term_obj.compute(cr,
                        uid, inv.payment_term.id, total, inv.date_invoice or False, context=ctx)
            if totlines:
                res_amount_currency = total_currency
                i = 0
                ctx.update({'date': inv.date_invoice})
                for t in totlines:
                    if inv.currency_id.id != company_currency:
                        amount_currency = cur_obj.compute(cr, uid, company_currency, inv.currency_id.id, t[1], context=ctx)
                    else:
                        amount_currency = False

                    # last line add the diff
                    res_amount_currency -= amount_currency or 0
                    i += 1
                    if i == len(totlines):
                        amount_currency += res_amount_currency

                    iml.append({
                        'type': 'dest',
                        'name': name,
                        'price': t[1],
                        'account_id': acc_id,
                        'date_maturity': t[0],
                        'amount_currency': diff_currency_p \
                                and amount_currency or False,
                        'currency_id': diff_currency_p \
                                and inv.currency_id.id or False,
                        'ref': ref,
                    })
            else:
                iml.append({
                    'type': 'dest',
                    'name': name,
                    'price': total,
                    'account_id': acc_id,
                    'date_maturity': inv.date_due or False,
                    'amount_currency': diff_currency_p \
                            and total_currency or False,
                    'currency_id': diff_currency_p \
                            and inv.currency_id.id or False,
                    'ref': ref
            })

            date = inv.date_invoice or time.strftime('%Y-%m-%d')

            part = self.pool.get("res.partner")._find_accounting_partner(inv.partner_id)

            line = map(lambda x:(0,0,self.line_get_convert(cr, uid, x, part.id, date, context=ctx)),iml)

            line = self.group_lines(cr, uid, iml, line, inv)

            journal_id = inv.journal_id.id
            journal = journal_obj.browse(cr, uid, journal_id, context=ctx)
            if journal.centralisation:
                raise osv.except_osv(_('User Error!'),
                        _('You cannot create an invoice on a centralized journal. Uncheck the centralized counterpart box in the related journal from the configuration menu.'))

            line = self.finalize_invoice_move_lines(cr, uid, inv, line)

            move = {
                'ref': inv.reference and inv.reference or inv.name,
                'line_id': line,
                'journal_id': journal_id,
                'date': date,
                'narration': inv.comment,
                'company_id': inv.company_id.id,
            }
            period_id = inv.period_id and inv.period_id.id or False
            ctx.update(company_id=inv.company_id.id,
                       account_period_prefer_normal=True)
            if not period_id:
                period_ids = period_obj.find(cr, uid, inv.date_invoice, context=ctx)
                period_id = period_ids and period_ids[0] or False
            if period_id:
                move['period_id'] = period_id
                for i in line:
                    i[2]['period_id'] = period_id

            ctx.update(invoice=inv)
            move_id = move_obj.create(cr, uid, move, context=ctx)
            new_move_name = move_obj.browse(cr, uid, move_id, context=ctx).name
            # make the invoice point to that move
            self.write(cr, uid, [inv.id], {'move_id': move_id,'period_id':period_id, 'move_name':new_move_name}, context=ctx)
            # Pass invoice in context in method post: used if you want to get the same
            # account move reference when creating the same invoice after a cancelled one:
#            move_obj.post(cr, uid, [move_id], context=ctx)
        self._log_event(cr, uid, ids)
        return True

    def _check_fiscal_values(self, cr, uid, inv):
        # Si es factura de cliente
        denomination_id = inv.denomination_id and inv.denomination_id.id or False
        if inv.type in ('out_invoice', 'out_refund'):

            if not denomination_id:
                raise osv.except_osv(_('Error!'), _('Denomination not set in invoice'))

            if inv.pos_ar_id.denomination_id.id != denomination_id:
                raise osv.except_osv(_('Error!'), _('Point of sale has not the same denomination as the invoice.'))

            # Chequeamos que la posicion fiscal y la denomination_id coincidan
            if inv.fiscal_position.denomination_id.id != denomination_id:
                raise osv.except_osv( _('Error'),
                            _('The invoice denomination does not corresponds with this fiscal position.'))

        # Si es factura de proveedor
        else:
            if not denomination_id:
                raise osv.except_osv(_('Error!'), _('Denomination not set in invoice'))

            # Chequeamos que la posicion fiscal y la denomination_id coincidan
            if inv.fiscal_position.denom_supplier_id.id != inv.denomination_id.id:
                raise osv.except_osv( _('Error'),
                                    _('The invoice denomination does not corresponds with this fiscal position.'))

        # Chequeamos que la posicion fiscal de la factura y del cliente tambien coincidan
        if inv.fiscal_position.id != inv.partner_id.property_account_position.id:
            raise osv.except_osv( _('Error'),
                                _('The invoice fiscal position is not the same as the partner\'s fiscal position.'))

        return True

    def _get_next_wsfe_number(self, cr, uid, conf, inv, context=None):
        voucher_type_obj = self.pool.get('wsfe.voucher_type')
        conf_obj = conf._model

        # Obtenemos el tipo de comprobante
        tipo_cbte = voucher_type_obj.get_voucher_type(cr, uid, inv, context=context)
        try:
            pto_vta = int(inv.pos_ar_id.name)
        except ValueError:
            raise osv.except_osv('Error', 'El nombre del punto de venta tiene que ser numerico')

        res = conf_obj.get_last_voucher(cr, uid, [conf.id], pto_vta, tipo_cbte, context=context)
        return res+1

    def get_next_invoice_number(self, cr, uid, invoice, context=None):
        """Funcion para obtener el siguiente numero de comprobante correspondiente en el sistema"""

        # Obtenemos el ultimo numero de comprobante para ese pos y ese tipo de comprobante
        cr.execute("select max(to_number(substring(internal_number from '[0-9]{8}$'), '99999999')) from account_invoice where internal_number ~ '^[0-9]{4}-[0-9]{8}$' and pos_ar_id=%s and state in %s and type=%s and is_debit_note=%s", (invoice.pos_ar_id.id, ('open', 'paid', 'cancel',), invoice.type, invoice.is_debit_note))
        last_number = cr.fetchone()

        # Si no devuelve resultados, es porque es el primero
        if not last_number or not last_number[0]:
            next_number = 1
        else:
            next_number = last_number[0] + 1

        return next_number


    def action_number(self, cr, uid, ids, context=None):
        wsfe_conf_obj = self.pool.get('wsfe.config')
        wsfe_conf = wsfe_conf_obj.get_config(cr, uid)

        wsfex_conf_obj = self.pool.get('wsfex.config')
        wsfex_conf = wsfex_conf_obj.get_config(cr, uid)

        if not context:
            context = {}

        next_number = None
        invoice_vals = {}
        invtype = None

        #TODO: not correct fix but required a fresh values before reading it.
        # Esto se usa para forzar a que recalcule los campos funcion
        self.write(cr, uid, ids, {})

        for obj_inv in self.browse(cr, uid, ids, context=context):
            partner_country = obj_inv.partner_id.country_id and obj_inv.partner_id.country_id.id or False
            company_country = obj_inv.company_id.country_id and obj_inv.company_id.country_id.id or False

            id = obj_inv.id
            invtype = obj_inv.type

            # Chequeamos si es local por medio de la posicion fiscal
            local = True
            local = obj_inv.fiscal_position.local

            reference = obj_inv.reference or ''

            # Si es local o de cliente
            if local or invtype in ('out_invoice', 'out_refund'):
                # Chequeamos los valores fiscales
                self._check_fiscal_values(cr, uid, obj_inv)

            # si el usuario no ingreso un numero, busco el ultimo y lo incremento , si no hay ultimo va 1.
            # si el usuario hizo un ingreso dejo ese numero
            internal_number = obj_inv.internal_number #False
            next_number = False

            # Si son de Cliente
            if invtype in ('out_invoice', 'out_refund'):

                pos_ar = obj_inv.pos_ar_id
                next_number = self.get_next_invoice_number(cr, uid, obj_inv, context=context)

                # Chequeamos si corresponde Factura Electronica
                # Aca nos fijamos si el pos_ar_id tiene factura electronica asignada
                confs = filter(lambda c: pos_ar in c.point_of_sale_ids, [wsfe_conf, wsfex_conf]) #_get_ws_conf(obj_inv.pos_ar_id)

                if len(confs)>1:
                    raise osv.except_osv(_("WSFE Error"), _("There is more than one configuration with this POS %s") % pos_ar.name)

                if confs:
                    conf = confs[0]
                    invoice_vals['aut_cae'] = True
                    fe_next_number = self._get_next_wsfe_number(cr, uid, conf, obj_inv, context=context)

                    # Si es homologacion, no hacemos el chequeo del numero
                    if not conf.homologation:
                        if fe_next_number != next_number:
                            raise osv.except_osv(_("WSFE Error!"), _("The next number [%d] does not corresponds to that obtained from AFIP WSFE [%d]") % (int(next_number), int(fe_next_number)))
                    else:
                        next_number = fe_next_number

                # Si no es Factura Electronica...
                else:
                    # Nos fijamos si el usuario dejo en blanco el campo de numero de factura
                    if obj_inv.internal_number:
                        internal_number = obj_inv.internal_number


                # Lo ponemos como en Proveedores, o sea, A0001-00000001
                if not internal_number:
                    internal_number = '%s-%08d' % (pos_ar.name, next_number)

                m = re.match('^[0-9]{4}-[0-9]{8}$', internal_number)
                if not m:
                    raise osv.except_osv( _('Error'), _('The Invoice Number should be the format XXXX-XXXXXXXX'))

                # Escribimos el internal number
                invoice_vals['internal_number'] = internal_number

            # Si son de Proveedor
            else:
                if not obj_inv.internal_number:
                    raise osv.except_osv( _('Error'), _('The Invoice Number should be filled'))

                if local:
                    m = re.match('^[0-9]{4}-[0-9]{8}$', obj_inv.internal_number)
                    if not m:
                        raise osv.except_osv( _('Error'), _('The Invoice Number should be the format XXXX-XXXXXXXX'))


            # Escribimos los campos necesarios de la factura
            self.write(cr, uid, obj_inv.id, invoice_vals)

            invoice_name = self.name_get(cr, uid, [obj_inv.id])[0][1]
            if not reference:
                ref = invoice_name
            else:
                ref = '%s [%s]' % (invoice_name, reference)

            # Actulizamos el campo reference del move_id correspondiente a la creacion de la factura
            self._update_reference(cr, uid, obj_inv, ref, context=context)

            # Como sacamos el post de action_move_create, lo tenemos que poner aqui
            # Lo sacamos para permitir la validacion por lote. Ver wizard account.invoice.confirm
            move_id = obj_inv.move_id and obj_inv.move_id.id or False
            self.pool.get('account.move').post(cr, uid, [move_id], context={'invoice':obj_inv})

            for inv_id, name in self.name_get(cr, uid, [id], context=context):
                ctx = context.copy()
                if invtype in ('out_invoice', 'out_refund'):
                    ctx = self.get_log_context(cr, uid, context=ctx)
                message = _('Invoice ') + " '" + name + "' "+ _("is validated.")
                self.log(cr, uid, inv_id, message, context=ctx)

        return True

#    def wsfe_invoice_prepare_detail(self, cr, uid, ids, conf, context=None):
#        conf_obj = conf._model
#
#        details = conf_obj.prepare_details(cr, uid, conf, ids, context=context)
#        return details


    def hook_add_taxes(self, cr, uid, inv, detalle):
        return detalle

    def action_aut_cae(self, cr, uid, ids, context={}, *args):
        voucher_type_obj = self.pool.get('wsfe.voucher_type')

        wsfe_conf_obj = self.pool.get('wsfe.config')
        wsfe_conf = wsfe_conf_obj.get_config(cr, uid)

        wsfex_conf_obj = self.pool.get('wsfex.config')
        wsfex_conf = wsfex_conf_obj.get_config(cr, uid)

        for inv in self.browse(cr, uid, ids):
            if not inv.aut_cae:
                #self.write(cr, uid, ids, {'cae' : 'NA'})
                return True

            pos_ar = inv.pos_ar_id
            # Chequeamos si corresponde Factura Electronica
            # Aca nos fijamos si el pos_ar_id tiene factura electronica asignada
            confs = filter(lambda c: pos_ar in c.point_of_sale_ids, [wsfe_conf, wsfex_conf]) #_get_ws_conf(obj_inv.pos_ar_id)

            if len(confs)>1:
                raise osv.except_osv(_("WSFE Error"), _("There is more than one configuration with this POS %s") % pos_ar.name)

            if confs:
                conf = confs[0]
            else:
                raise osv.except_osv(_("WSFE Error"), _("There is no configuration for this POS %s") % pos_ar.name)

            conf_obj = conf._model

            # Obtenemos el tipo de comprobante
            tipo_cbte = voucher_type_obj.get_voucher_type(cr, uid, inv, context=context)

            # Obtenemos el numero de comprobante a enviar a la AFIP teniendo en
            # cuenta que inv.number == 000X-00000NN o algo similar.
            # TODO: Esto esta duplicado en los metodos de wsfe y wsfex
            inv_number = inv.internal_number
            pos, cbte_nro = inv_number.split('-')
            pos = int(pos)
            cbte_nro = int(cbte_nro)

            # Derivamos a la configuracion correspondiente
            fe_det_req = conf_obj.prepare_details(cr, uid, conf, ids, context=context)
            result = conf_obj.get_invoice_CAE(cr, uid, [conf.id], [inv.id], pos, tipo_cbte, fe_det_req, context=context)

            new_cr = False
            try:
                invoices_approbed = conf_obj._parse_result(cr, uid, [conf.id], ids, result, context=context)
                for invoice_id, invoice_vals in invoices_approbed.iteritems():
                    self.write(cr, uid, invoice_id, invoice_vals)
            except Exception, e:
                new_cr = cr.dbname
                cr.rollback()
                raise e
            finally:
                # Creamos el wsfe.request con otro cursor, porque puede pasar que
                # tengamos una excepcion e igualmente, tenemos que escribir la request
                # Sino al hacer el rollback se pierde hasta el wsfe.request
                if new_cr:
                    cr2 = pooler.get_db(new_cr).cursor()
                else:
                    cr2 = cr

                conf_obj._log_wsfe_request(cr2, uid, ids, pos, tipo_cbte, fe_det_req, result)
                if new_cr:
                    cr2.commit()
                    cr2.close()

        return True

    def _parse_result(self, cr, uid, ids, result, context=None):

        if not context:
            context = {}

        invoices_approbed = {}

        # Verificamos el resultado de la Operacion
        # Si no fue aprobado
        if result['Resultado'] == 'R':
            msg = ''
            if result['Errores']:
                msg = 'Errores: ' + '\n'.join(result['Errores']) + '\n'

            if context.get('raise-exception', True):
                raise osv.except_osv(_('AFIP Web Service Error'),
                                     _('La factura no fue aprobada. \n%s') % msg)

        elif result['Resultado'] == 'A' or result['Resultado'] == 'P':
            index = 0
            for inv in self.browse(cr, uid, ids):
                invoice_vals = {}
                comp = result['Comprobantes'][index]
                if comp['Observaciones']:
                    msg = 'Observaciones: ' + '\n'.join(comp['Observaciones'])

                    ## Escribimos en el log del cliente web
                    #self.log(cr, uid, inv.id, msg, context)

                # Chequeamos que se corresponda con la factura que enviamos a validar
                doc_type = inv.partner_id.document_type_id and inv.partner_id.document_type_id.afip_code or '99'
                doc_tipo = comp['DocTipo'] == int(doc_type)
                doc_num = comp['DocNro'] == int(inv.partner_id.vat)
                cbte = True
                if inv.internal_number:
                    cbte = comp['CbteHasta'] == int(inv.internal_number.split('-')[1])
                else:
                    # TODO: El nro de factura deberia unificarse para que se setee en una funcion
                    # o algo asi para que no haya posibilidad de que sea diferente nunca en su formato
                    invoice_vals['internal_number'] = '%04d-%08d' % (result['PtoVta'], comp['CbteHasta'])

                if not all([doc_tipo, doc_num, cbte]):
                    raise osv.except_osv(_("WSFE Error!"), _("Validated invoice that not corresponds!"))

                if comp['Resultado'] == 'A':
                    invoice_vals['cae'] = comp['CAE']
                    invoice_vals['cae_due_date'] = comp['CAEFchVto']
                    invoices_approbed[inv.id] = invoice_vals
                    #self.write(cr, uid, inv.id, invoice_vals)
                    #invoices_approbed.append(inv.id)

                index += 1

        return invoices_approbed

account_invoice()


class account_invoice_tax(osv.osv):
    _name = "account.invoice.tax"
    _inherit = "account.invoice.tax"


    def hook_compute_invoice_taxes(self, cr, uid, invoice_id, tax_grouped, context=None):
        tax_obj = self.pool.get('account.tax')
        cur_obj = self.pool.get('res.currency')
        inv = self.pool.get('account.invoice').browse(cr, uid, invoice_id, context=context)
        cur = inv.currency_id

        for t in tax_grouped.values():
            # Para solucionar el problema del redondeo con AFIP
            ta = tax_obj.browse(cr, uid, t['tax_id'], context=context)
            t['amount'] = t['base']*ta.amount
            t['tax_amount'] = t['base_amount']*ta.amount

            t['base'] = cur_obj.round(cr, uid, cur, t['base'])
            t['amount'] = cur_obj.round(cr, uid, cur, t['amount'])
            t['base_amount'] = cur_obj.round(cr, uid, cur, t['base_amount'])
            t['tax_amount'] = cur_obj.round(cr, uid, cur, t['tax_amount'])

        return super(account_invoice_tax, self).hook_compute_invoice_taxes(cr, uid, invoice_id, tax_grouped, context)

account_invoice_tax()
