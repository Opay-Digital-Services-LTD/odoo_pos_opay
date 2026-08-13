# from odoo import models, fields, api


# class pos_opay(models.Model):
#     _name = 'pos_opay.pos_opay'
#     _description = 'pos_opay.pos_opay'

#     name = fields.Char()
#     value = fields.Integer()
#     value2 = fields.Float(compute="_value_pc", store=True)
#     description = fields.Text()
#
#     @api.depends('value')
#     def _value_pc(self):
#         for record in self:
#             record.value2 = float(record.value) / 100

