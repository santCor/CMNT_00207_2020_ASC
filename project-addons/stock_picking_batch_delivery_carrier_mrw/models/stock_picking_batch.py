##############################################################################
#    License AGPL-3 - See http://www.gnu.org/licenses/agpl-3.0.html
#    Copyright (C) 2020 Comunitea Servicios Tecnológicos S.L. All Rights Reserved
#    Vicente Ángel Gutiérrez <vicente@comunitea.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See thefire
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import logging
import base64
from datetime import datetime

from requests import Session

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from zeep import Client
from zeep.cache import SqliteCache
from zeep.plugins import HistoryPlugin
from zeep.transports import Transport

_logger = logging.getLogger(__name__)


class StockBatchPicking(models.Model):

    _inherit = "stock.picking.batch"

    mrw_pdo_quantity = fields.Float("PDO amount")

    @api.multi
    def write(self, vals):
        res = super().write(vals)
        if self.carrier_id.code == "MRW":
            if vals.get("carrier_id", False):
                self.onchange_carrier_id()
            return res

    @api.onchange("carrier_id")
    def onchange_carrier_id(self):
        if self.carrier_id.code == "MRW":
            pickings_total_value = 0.0
            for pick in self.picking_ids:
                pickings_total_value += pick.amount_total
            self.mrw_pdo_quantity = pickings_total_value

    def create_client(self):
        session = Session()
        session.verify = False

        try:
            transport = Transport(cache=SqliteCache(), session=session)
            history = HistoryPlugin()
            if self.carrier_id.account_id.test_enviroment:
                url = self.carrier_id.account_id.service_test_url
            else:
                url = self.carrier_id.account_id.service_url
            client = Client(url, transport=transport, plugins=[history])

            if client:
                return client, history
            else:
                raise AccessError(_("Not possible to establish a client."))
        except Exception as e:
            raise AccessError(_("Access error message: {}".format(e)))

    def setMRWHeaders(self, client):

        try:
            element_type = client.get_element("{http://www.mrw.es/}AuthInfo")
            headers = element_type(
                CodigoFranquicia=self.carrier_id.account_id.mrw_franchise,
                CodigoAbonado=self.carrier_id.account_id.mrw_account,
                CodigoDepartamento="",
                UserName=self.carrier_id.account_id.account,
                Password=self.carrier_id.account_id.password,
            )
            return headers
        except Exception:
            return False

    def get_carrier_label(self, client, headers, numeroEnvio):
        EtiquetaEnvio = {
            "request": {
                "NumeroEnvio": numeroEnvio,
                "SeparadorNumerosEnvio": "",
                "ReportTopMargin": 1100,
                "ReportLeftMargin": 650,
            }
        }

        label = client.service.EtiquetaEnvio(
            **EtiquetaEnvio, _soapheaders=[headers]
        )
        return label

    def send_shipping(self):
        super(StockBatchPicking, self).send_shipping()
        if self.carrier_id.code == "MRW":

            if not self.carrier_id.account_id:
                raise UserError("Delivery carrier has no account.")

            client, history = self.create_client()

            if client:

                try:

                    headers = self.setMRWHeaders(client)

                    notices = []
                    if self.carrier_id.account_id.mrw_phone_notification:
                        phone_notification = {
                            "NotificacionRequest": {
                                "CanalNotificacion": "2",
                                "TipoNotificacion": self.carrier_id.account_id.mrw_notice_type,
                                "MailSMS": self.partner_id.phone
                                or self.partner_id.mobile,
                            }
                        }
                        notices.append(phone_notification)

                    if self.carrier_id.account_id.mrw_mail_notification:
                        mail_notification = {
                            "NotificacionRequest": {
                                "CanalNotificacion": "1",
                                "TipoNotificacion": self.carrier_id.account_id.mrw_notice_type,
                                "MailSMS": self.partner_id.email,
                            }
                        }
                        notices.append(mail_notification)

                    TransmEnvio = {
                        "request": {
                            "DatosEntrega": {
                                "Direccion": {
                                    "Via": "{} {}".format(
                                        self.partner_id.street or "",
                                        self.partner_id.street2 or "",
                                    ),
                                    "CodigoPostal": self.partner_id.zip,
                                    "Poblacion": self.partner_id.city,
                                    "Provincia": self.partner_id.state_id.name,
                                },
                                "Nif": self.partner_id.vat,
                                "Nombre": self.partner_id.name,
                                "Telefono": self.partner_id.phone
                                or self.partner_id.mobile
                                or "",
                                "Observaciones": "{}".format(
                                    self.delivery_note
                                ),
                            },
                            "DatosServicio": {
                                "Fecha": datetime.now().strftime(
                                    "%d/%m/%Y"
                                ),  # self.date.strftime("%d/%m/%Y"),
                                "Referencia": self.name,
                                "CodigoServicio": self.carrier_code.carrier_code,
                                "Frecuencia": self.carrier_id.account_id.mrw_frequency
                                if self.carrier_code.carrier_code == "0005"
                                else "",
                                "NumeroBultos": self.carrier_packages,
                                "Peso": round(self.carrier_weight),
                                "EntregaSabado": self.carrier_id.account_id.mrw_saturday_delivery,
                                "Entrega830": self.carrier_id.account_id.mrw_830_delivery,
                                "Gestion": self.carrier_id.account_id.mrw_delivery_hangle,
                                "ConfirmacionInmediata": self.carrier_id.account_id.mrw_instant_notice,
                                "Reembolso": self.carrier_id.account_id.mrw_delivery_pdo
                                if self.payment_on_delivery
                                else "N",
                                "ImporteReembolso": self.mrw_pdo_quantity
                                if self.carrier_id.account_id.mrw_delivery_pdo
                                != "N"
                                else "",
                                "TipoMercancia": self.carrier_id.account_id.mrw_goods_type,
                                "Notificaciones": notices,
                            },
                        }
                    }

                    res = client.service.TransmEnvio(
                        **TransmEnvio, _soapheaders=[headers]
                    )
                except Exception as e:
                    raise AccessError(_("Access error message: {}").format(e))

                if res["Estado"] == "0":
                    raise AccessError(
                        _("Error message: {}").format(res["Mensaje"])
                    )
                elif res["Estado"] == "1":

                    self.write(
                        {
                            "carrier_tracking_ref": res["NumeroEnvio"],
                            "shipment_reference": res["NumeroSolicitud"],
                        }
                    )

                    try:
                        label = self.get_carrier_label(
                            client, headers, res["NumeroEnvio"]
                        )
                    except Exception as e:
                        _logger.error(
                            _(
                                "Connection error: {}, while trying to retrieve the label."
                            ).format(e)
                        )
                        return

                    if label["Estado"] == "1":
                        file_b64 = base64.b64encode(label["EtiquetaFile"])
                        self.env["ir.attachment"].create(
                            {
                                "name": "Label: {}".format(self.name),
                                "type": "binary",
                                "datas": file_b64,
                                "datas_fname": "Label" + self.name + ".pdf",
                                "store_fname": self.name,
                                "res_model": self._name,
                                "res_id": self.id,
                                "mimetype": "application/x-pdf",
                            }
                        )
                        self.print_created_labels()
                    elif label["Estado"] == "0":
                        raise AccessError(
                            _(
                                "Error while trying to retrieve the label: {}"
                            ).format(label["Mensaje"])
                        )
                    else:
                        raise AccessError(
                            _("Error while trying to retrieve the label")
                        )
                else:
                    raise AccessError(
                        _("Unknown error with the SOAP connection.")
                    )

            else:
                raise AccessError(_("Not possible to establish a client."))

    def check_delivery_address(self):
        super(StockBatchPicking, self).check_delivery_address()
        if self.carrier_id.code == "MRW":
            if not self.partner_id.state_id:
                raise UserError(
                    _("Partner address is not complete (State missing).")
                )


class StockPicking(models.Model):

    _inherit = "stock.picking"

    def check_shipment_status(self):
        if self.carrier_id.code == "DHL":
            if not self.carrier_id.account_id:
                raise UserError("Delivery carrier has no account.")

            client, history = self.batch_id.create_client()

            if not client:
                raise AccessError(_("Not possible to establish a client."))

            try:
                GetEnvios = {
                    "login": self.carrier_id.account_id.mrw_tracking_user,
                    "pass": self.carrier_id.account_id.mrw_tracking_password,
                    "codigoIdioma": 3082,
                    "tipoFiltro": 1,
                    "valorFiltroDesde": self.carrier_tracking_ref,
                    "tipoInformacion": 0,
                    "codigoAbonado": self.carrier_id.account_id.mrw_account,
                    "codigoFranquicia": self.carrier_id.account_id.mrw_franchise,
                }

                res = client.service.GetEnvios(**GetEnvios)
            except Exception as e:
                raise AccessError(_("Access error message: {}").format(e))

            seguimiento = res["Seguimiento"]["Abonado"]["Seguimiento"]
            if seguimiento["Estado"] == "00":
                self.delivered = True
            else:
                raise AccessError(
                    _("Error: {}".format(res["MensajeSeguimiento"]))
                )

        return super(StockPicking, self).check_shipment_status()
