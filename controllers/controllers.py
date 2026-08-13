# from odoo import http


# class PosOpay(http.Controller):
#     @http.route('/pos_opay/pos_opay', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/pos_opay/pos_opay/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('pos_opay.listing', {
#             'root': '/pos_opay/pos_opay',
#             'objects': http.request.env['pos_opay.pos_opay'].search([]),
#         })

#     @http.route('/pos_opay/pos_opay/objects/<model("pos_opay.pos_opay"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('pos_opay.object', {
#             'object': obj
#         })

