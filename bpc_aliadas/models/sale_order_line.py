# -*- encoding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
from datetime import timedelta, date, datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged, Form
import logging

_logger = logging.getLogger(__name__)
RENTAL_TYPE = [
    ('fixed','Monto fijo'),
    ('m2','Monto por metro cuadrado'),
    ('consumption', 'Consumo'),
    ('consumption_min', 'Consumo mínimo'),
    ('consumption_fixed', 'Consumo y monto fijo'),
    ('rental_min', 'Renta monto mínimo'),
    ('rental_percentage', 'Renta % de ventas'),
    ('rental_percentage_top', 'Renta % de ventas con tope'),
    ('tonnage', 'Tonelaje'),
]


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    price_min = fields.Float('Precio mínimo', required=True, digits='Product Price', default=0.0, store=True, copy=False)
    approved_required = fields.Boolean(help='Requiere aprobación', store=True, copy=False)
    amount_min = fields.Float(string='Mínimo')
    amount_max = fields.Float(string='Máximo')
    percentage_sale = fields.Float(string='% sobre venta', help='Porcentaje sobre venta')
    rental_type = fields.Selection(RENTAL_TYPE, string='Variable')
    document_type_sale_id = fields.Many2one('document.type', domain=[('in_sale', '=', True)])

    def default_pricelist_order(self):
        if self.order_id.pricelist_id:
            return self.order_id.pricelist_id

    pricelist_id = fields.Many2one('product.pricelist', string='Tarifa', ondelete="cascade", default=default_pricelist_order)
    currency_external_id = fields.Many2one('res.currency', string='Moneda')


    def _default_currency_rate(self):
        currency_rate = 1
        if self.order_id.currency_id != self.env.company.currency_id:
            currency_rate = self.order_id.currency_rate
        return currency_rate
        #record.currency_rate = currency_rate

    currency_rate = fields.Float(store=True, string='T.Cambio', default=_default_currency_rate)  # TIPO DE CAMBIO
    #currency_rate = fields.Float(compute='_compute_currency_rate', store=True, string='T.Cambio')  # TIPO DE CAMBIO
    amount_convert = fields.Monetary(string='Conversión', compute='_compute_amount_convert', store=True)
    not_update_price = fields.Boolean(string='No actualizar precio')

    subscription_template_id = fields.Many2one('sale.subscription.template', string='Plantilla de susbcripción')

    pickup_date_format = fields.Date(string='Inicia')
    return_date_format = fields.Date(string='Termina')

    find_range = fields.Boolean(help="Encontrado por rango en tarifa")

    product_currency_invoice_id = fields.Many2one('res.currency', string='Moneda facturación producto', related='product_id.currency_invoice_id')



    # @api.constrains('pricelist_id')
    # def _constraint_pricelist_id(self):
    #     for record in self:
    #         if not record.pricelist_id:
    #             raise ValidationError(_("No se encontró Tariga para el producto %s con el precio de %s" % (record.product_id.name, record.price_unit)))

    def _get_display_price(self, product):
        """Ensure unit price isn't recomputed."""
        if self.is_rental:
            price = self._get_display_price_aliadas(product)
            return price
            # return self.price_unit
        else:
            return super(SaleOrderLine, self)._get_display_price(product)

    def _get_display_price_aliadas(self, product):
        # TO DO: move me in master/saas-16 on sale.order
        # awa: don't know if it's still the case since we need the "product_no_variant_attribute_value_ids" field now
        # to be able to compute the full price

        # it is possible that a no_variant attribute is still in a variant if
        # the type of the attribute has been changed after creation.
        no_variant_attributes_price_extra = [
            ptav.price_extra for ptav in self.product_no_variant_attribute_value_ids.filtered(
                lambda ptav:
                ptav.price_extra and
                ptav not in product.product_template_attribute_value_ids
            )
        ]
        if no_variant_attributes_price_extra:
            product = product.with_context(
                no_variant_attributes_price_extra=tuple(no_variant_attributes_price_extra)
            )

        if self.order_id.pricelist_id.discount_policy == 'with_discount':
            return product.with_context(pricelist=self.order_id.pricelist_id.id, uom=self.product_uom.id).price
        product_context = dict(self.env.context, partner_id=self.order_id.partner_id.id, date=self.order_id.date_order, uom=self.product_uom.id)

        final_price, rule_id = self.order_id.pricelist_id.with_context(product_context).get_product_price_rule(product or self.product_id, self.product_uom_qty or 1.0, self.order_id.partner_id)
        base_price, currency = self.with_context(product_context)._get_real_price_currency(product, rule_id, self.product_uom_qty, self.product_uom, self.order_id.pricelist_id.id)
        if currency != self.order_id.pricelist_id.currency_id:
            base_price = currency._convert(
                base_price, self.order_id.pricelist_id.currency_id,
                self.order_id.company_id or self.env.company, self.order_id.date_order or fields.Date.today())
        # negative discounts (= surcharge) are included in the display price
        return max(base_price, final_price)

    def _timesheet_service_generation(self):
        order_id = self.mapped('order_id')
        if order_id.not_project and not order_id.perm_can_project and not order_id.from_maintenance:
            _logger.info("ALIADAS: NO debería crear proyecto")
            pass
        else:
            _logger.info("ALIADAS: SI debería crear proyecto")
            return super(SaleOrderLine, self)._timesheet_service_generation()

    def _update_taxes(self):
        super(SaleOrderLine, self)._update_taxes()
        if self.product_id:
            #self.price_min = self.price_unit
            if self.product_id.meter2 not in (False, 0.0):
                self.product_uom_qty = self.product_id.meter2

    @api.onchange('product_id')
    def product_id_change(self):
        if self.product_id.id == self.env.ref('bpc_aliadas.rental_product_bpc').id and not self.env.user.user_has_groups('bpc_aliadas.group_aliadas_sale_subs_product_rent_default'):
            raise ValidationError(_("No tiene permiso para seleccionar este producto."))

        res = super(SaleOrderLine, self).product_id_change()
        if self.product_id and self.rental_type == 'm2':
            order_id = self.order_id
            lines_local = order_id.order_line._filtered_local()
            lines_local = self.line_product_not_repeat_local(lines_local)
            total = sum(line.product_uom_qty for line in lines_local)
            self.product_uom_qty = total
        if self.product_id:
            self.rental_type = self.product_id.rental_type
            self.document_type_sale_id = self.product_id.document_type_sale_id
            self.subscription_template_id = self.product_id.subscription_template_id
        #Lista de precios
        self._compute_pricelist_line()
        self.pricelist_id = self.order_id.pricelist_id
        return res

    def line_product_not_repeat_local(self, lines_local):
        locals = self.env['sale.order.line'].sudo()
        if lines_local:
            locals += lines_local[0]
            others = lines_local - locals
            if others:
                for other in others:
                    _find = locals.filtered(lambda x: x.product_id == other.product_id)
                    if not _find:
                        locals += other

        return locals


    # @api.onchange('rental_type')
    # def onchange_rental_type(self):
    #     if self.product_id and self.rental_type == 'm2':
    #         order_id = self.order_id
    #         lines_local = order_id.order_line._filtered_local()
    #         total = sum(line.product_uom_qty for line in lines_local)
    #         self.product_uom_qty = total
    # @api.depends('price_unit')
    # def _compute_pricelist_id(self):
    #     for record in self:
    #

    @api.onchange('pricelist_id')
    def _onchange_pricelist_id(self):
        for record in self:
            record._compute_pricelist_line()

    def _compute_pricelist_line(self):
        for record in self:
            self.currency_external_id = record.pricelist_id.currency_id
            record._onchange_currency_external_id()


    @api.onchange('currency_external_id')
    def _onchange_currency_external_id(self):
        for record in self:
            if record.currency_external_id == record.env.company.currency_id:
                record.currency_rate = 1
            else:
                record.currency_rate = record.order_id.currency_rate

    # @api.depends('currency_external_id', 'order_id.date_order')
    # def _compute_currency_rate(self):
    #     for record in self:
    #         #currency_rate = record.price_subtotal
    #         currency_rate = 1
    #         if record.order_id.currency_id != record.currency_external_id:
    #             currency_rate = record.currency_external_id._convert(currency_rate, record.order_id.currency_id, record.company_id, record.order_id.date_order.date() or datetime.now().date())
    #         record.currency_rate = currency_rate



    @api.depends('currency_rate', 'price_subtotal')
    def _compute_amount_convert(self):
        for record in self:
            amount_convert = record.price_subtotal
            if record.order_id.currency_id != record.currency_external_id:
                amount_convert = record.order_id.currency_id._convert(amount_convert, record.currency_external_id, record.company_id, record.order_id.date_order.date() or datetime.now().date())
            record.amount_convert = amount_convert

    def _find_pricelist_by_price(self, price_unit):
        self.ensure_one()
        item = self.env['product.pricelist.item'].sudo()
        pricelist_id = False
        _logger.info("Buscando lista de precios por cambio de precio unitario. Producto %s - precio %s " % (self.product_id.name, price_unit))
        if self.product_id and price_unit >= 0.0:

            items = item.search(['|', '|',
                                 ('product_tmpl_id', '=', self.product_id.product_tmpl_id.id),
                                 ('product_id', 'in', self.product_id.ids),
                                 ('categ_id', '=', self.product_id.product_tmpl_id.categ_id.id),
                                 ])

            if self.product_id.check_not_analytic:
                if self.order_id.analytic_account_id:
                    find_items = items.filtered(lambda i: i.price_min <= price_unit <= i.price_max and
                                                          i.pricelist_id.analytic_account_id == self.order_id.analytic_account_id)
                else:
                    find_items = items.filtered(lambda i: i.price_min <= price_unit <= i.price_max)
            else:
                find_items = items.filtered(lambda i: i.price_min <= price_unit <= i.price_max and
                                         i.pricelist_id.analytic_account_id == self.product_id.product_tmpl_id.analytic_account_id)
            if find_items:
                pricelist_id = find_items[0].pricelist_id
                _logger.info("Lista de precio encontrada : %s " % pricelist_id.name)
                #find = True
                #self.pricelist_id = pricelist_id
                #self.find_range = find
            else:
                _logger.info("Lista de precio NO encontrada ")
                text = "No se encontro lista de precios para la línea con el producto %s, con la cuenta analítica %s y el precio unitario %s " \
                       % (self.product_id.name, self.product_id.product_tmpl_id.analytic_account_id.name, price_unit)
                #raise ValidationError(_(text))

        return pricelist_id


    @api.depends('product_uom_qty', 'discount', 'price_unit', 'tax_id')
    def _compute_amount(self):
        """
        TODO: REFACTOR
        Compute the amounts of the SO line.
        """
        for line in self:
            is_local = line._filtered_local()
            _logger.info("Es local ? %s " % is_local)
            pricelist_id = line._find_pricelist_by_price(line.price_unit)
            # if not pricelist_id:
            #     raise ValidationError(_("No se encontró lista de precios para producto %s " % line.product_id.name))
            #line.pricelist_id = pricelist_id
            find_range = True if pricelist_id else False
            _logger.info("Econtrado por rango en tarifa : %s" % find_range)
            price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            if line.rental_type == 'fixed':
                qty = 1
            else:
                qty = line.product_uom_qty
            taxes = line.tax_id.compute_all(price, line.order_id.currency_id, qty, product=line.product_id, partner=line.order_id.partner_shipping_id)
            line.update({
                'price_tax': sum(t.get('amount', 0.0) for t in taxes.get('taxes', [])),
                'price_total': taxes['total_included'],
                'price_subtotal': taxes['total_excluded'],
                'pricelist_id': pricelist_id.id if pricelist_id else False,
                'find_range': True if pricelist_id else False,
            })
            if self.env.context.get('import_file', False) and not self.env.user.user_has_groups('account.group_account_manager'):
                line.tax_id.invalidate_cache(['invoice_repartition_line_ids'], [line.tax_id.id])

            if is_local and line.pricelist_id:
                _logger.info("Actualizando tarifa de la orden según el local")
                line.order_id.pricelist_id = line.pricelist_id
                #line.order_id.order_line.filtered(lambda l: l.sudo().write({'pricelist_id': line.pricelist_id.id}))
            elif not is_local and not line.pricelist_id:
                line.update({
                    'pricelist_id': line.order_id.pricelist_id.id if line.order_id.pricelist_id else False
                })
            # if find:
            #     return {
            #         'type': 'ir.actions.client',
            #         'tag': 'display_notification',
            #         'params': {
            #             'type': 'success',
            #             'title': _('Bien!'),
            #             'message': _("Se encontró la tarifa"),
            #             'next': {'type': 'ir.actions.act_window_close'},
            #         }
            #     }
    # @api.onchange('price_unit')
    # def _onchange_price_unit(self):
    #     for record in self:
    #         if record.price_unit < record.price_min:
    #             # Linea que requiere aprobación
    #             record.approved_required = True
    #         else:
    #             record.approved_required = False

    @api.model
    def _filtered_local(self):
        lines = self.env['sale.order.line'].sudo()
        product_rental_id = self.env.ref('bpc_aliadas.rental_product_bpc')
        for record in self:
            if record.product_id.rent_ok and record.product_id.id != product_rental_id.id and not record.product_id.is_free:
                lines += record
        return lines

    def _filtered_rental(self):
        lines = self.env['sale.order.line'].sudo()
        product_rental_id = self.env.ref('bpc_aliadas.rental_product_bpc')
        for record in self:
            if record.product_id.id == product_rental_id.id:
                lines += record
        return lines

    # TODO: @Overrite
    def _prepare_subscription_line_data(self):
        # values = super(SaleOrder, self)._prepare_subscription_line_data()
        """Prepare a dictionnary of values to add lines to a subscription."""
        values = list()
        for line in self:
            values.append((0, False, {
                'product_id': line.product_id.id,
                'rental_type': line.rental_type,
                'document_type_sale_id': line.document_type_sale_id.id if line.document_type_sale_id else False,
                'currency_external_id': line.currency_external_id.id if line.currency_external_id else False,
                'currency_pricelist': line.pricelist_id.currency_id.id if line.pricelist_id and line.pricelist_id.currency_id else False,
                'currency_rate': line.currency_rate,
                'amount_convert': line.amount_convert,
                'name': line.name,
                'quantity': line.product_uom_qty,
                'uom_id': line.product_uom.id,
                'price_unit': line.price_unit,
                'discount': line.discount if line.order_id.subscription_management != 'upsell' else False,
                'order_line_id': line.id,
                'date_init': line.pickup_date + timedelta(hours=6) if line.pickup_date else False,
                'amount_consumption': 0.0,
                'amount_sale': 0.0,
                'amount_min': line.amount_min,
                'amount_max': line.amount_max,
                'percentage_sale': line.percentage_sale,
                'pickup_start': (line.pickup_date + timedelta(hours=6)).date() if line.pickup_date else False,
                'pickup_end': (line.return_date + timedelta(hours=6)).date() if line.return_date else False,
                'tax_ids': [(6, 0, line.tax_id.ids)] if line.tax_id else False
            }))
        return values

    # @api.onchange('pricelist_id')
    # def _onchange_pricelist_id(self):
    #     for record in self:
    #         record.update_pricelist()
    #
    # def update_pricelist(self):
    #     for record in self:
    #         if record.pricelist_id:
    #             price_unit = record.product_id._get_tax_included_unit_price(
    #                 record.company_id or record.order_id.company_id,
    #                 record.order_id.currency_id,
    #                 record.order_id.date_order,
    #                 'sale',
    #                 fiscal_position=record.order_id.fiscal_position_id,
    #                 product_price_unit=record._get_display_price_by_line(record.product_id),
    #                 product_currency=record.order_id.currency_id
    #             )
    #             record.price_unit = price_unit
                #record.price_min = price_unit


    def _get_display_price_by_line(self, product):
        # TO DO: move me in master/saas-16 on sale.order
        # awa: don't know if it's still the case since we need the "product_no_variant_attribute_value_ids" field now
        # to be able to compute the full price

        # it is possible that a no_variant attribute is still in a variant if
        # the type of the attribute has been changed after creation.
        pricelist_id = self.pricelist_id
        no_variant_attributes_price_extra = [
            ptav.price_extra for ptav in self.product_no_variant_attribute_value_ids.filtered(
                lambda ptav:
                    ptav.price_extra and
                    ptav not in product.product_template_attribute_value_ids
            )
        ]
        if no_variant_attributes_price_extra:
            product = product.with_context(
                no_variant_attributes_price_extra=tuple(no_variant_attributes_price_extra)
            )

        if pricelist_id.discount_policy == 'with_discount':
            return product.with_context(pricelist=pricelist_id.id, uom=self.product_uom.id).price
        product_context = dict(self.env.context, partner_id=self.order_id.partner_id.id, date=self.order_id.date_order, uom=self.product_uom.id)

        final_price, rule_id = self.pricelist_id.with_context(product_context).get_product_price_rule(product or self.product_id, self.product_uom_qty or 1.0, self.order_id.partner_id)
        base_price, currency = self.with_context(product_context)._get_real_price_currency(product, rule_id, self.product_uom_qty, self.product_uom, pricelist_id.id)
        if currency != pricelist_id.currency_id:
            base_price = currency._convert(
                base_price, self.order_id.pricelist_id.currency_id,
                self.order_id.company_id or self.env.company, self.order_id.date_order or fields.Date.today())
        # negative discounts (= surcharge) are included in the display price
        return max(base_price, final_price)

    @api.onchange('pickup_date_format','return_date_format')
    def _onchange_date_format(self):
        self.ensure_one()
        if self.pickup_date_format:
            pd = self.pickup_date_format
            self.pickup_date = datetime(pd.year, pd.month, pd.day, 0, 0, 0) + timedelta(hours=6)
            _logger.info("Segundo onchange")
        if self.return_date_format:
            rd = self.return_date_format
            self.return_date = datetime(rd.year, rd.month, rd.day, 0, 0, 0) + timedelta(hours=6)

    # def write(self, values):
    #     if 'pickup_date' in values:
    #         values['pickup_date_format'] = (values['pickup_date'] - timedelta(hours=6)).date()
    #         _logger.info("Fecha forma start : %s " % values['pickup_date_format'])
    #     if 'return_date' in values:
    #         values['return_date_format'] = (values['return_date'] - timedelta(hours=6)).date()
    #         _logger.info("Fecha forma end : %s " % values['return_date_format'])
    #     res = super(SaleOrderLine, self).write(values)
    #     return res

    @api.onchange('pickup_date', 'return_date')
    def _onchange_rental_info(self):
        super(SaleOrderLine, self)._onchange_rental_info()
        if self.pickup_date and not self.pickup_date_format:
            self.pickup_date_format = (self.pickup_date - timedelta(hours=6)).date()
            _logger.info("Primer onchange")
        if self.return_date and not self.return_date_format:
            self.return_date_format = (self.return_date - timedelta(hours=6)).date()
