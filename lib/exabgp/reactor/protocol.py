# encoding: utf-8
"""
protocol.py

Created by Thomas Mangin on 2009-08-25.
Copyright (c) 2009-2015 Exa Networks. All rights reserved.
"""

import os

import traceback

# ================================================================ Registration
#

from exabgp.reactor.network.outgoing import Outgoing
# from exabgp.reactor.network.error import NotifyError

from exabgp.protocol.family import AFI
from exabgp.protocol.family import SAFI
from exabgp.bgp.message import Message
from exabgp.bgp.message import NOP
from exabgp.bgp.message import _NOP
from exabgp.bgp.message import Open
from exabgp.bgp.message.open import Version
from exabgp.bgp.message.open import ASN
from exabgp.bgp.message.open import RouterID
from exabgp.bgp.message.open import HoldTime
from exabgp.bgp.message.open.capability import Capabilities
from exabgp.bgp.message.open.capability import Negotiated
from exabgp.bgp.message import Update
from exabgp.bgp.message import EOR
from exabgp.bgp.message import KeepAlive
from exabgp.bgp.message import Notification
from exabgp.bgp.message import Notify
from exabgp.bgp.message import Operational

from exabgp.reactor.api.processes import ProcessError

from exabgp.logger import Logger
from exabgp.logger import FakeLogger

# This is the number of chuncked message we are willing to buffer, not the number of routes
MAX_BACKLOG = 15000

_UPDATE = Update([],'')
_OPERATIONAL = Operational(0x00)


class Protocol (object):
	decode = True

	def __init__ (self, peer):
		try:
			self.logger = Logger()
		except RuntimeError:
			self.logger = FakeLogger()
		self.peer = peer
		self.neighbor = peer.neighbor
		self.negotiated = Negotiated(self.neighbor)
		self.connection = None
		port = os.environ.get('exabgp.tcp.port','') or os.environ.get('exabgp_tcp_port','')
		self.port = int(port) if port.isdigit() else 179

		# XXX: FIXME: check the the -19 is correct (but it is harmless)
		# The message size is the whole BGP message _without_ headers
		self.message_size = Message.MAX_LEN-Message.HEADER_LEN

		from exabgp.configuration.environment import environment
		self.log_routes = environment.settings().log.routes

	# XXX: we use self.peer.neighbor.peer_address when we could use self.neighbor.peer_address

	def __del__ (self):
		self.close('automatic protocol cleanup')

	def me (self, message):
		return "Peer %15s ASN %-7s %s" % (self.peer.neighbor.peer_address,self.peer.neighbor.peer_as,message)

	def accept (self, incoming):
		self.connection = incoming

		if self.peer.neighbor.api['neighbor-changes']:
			self.peer.reactor.processes.connected(self.peer.neighbor)

		# very important - as we use this function on __init__
		return self

	def connect (self):
		# allows to test the protocol code using modified StringIO with a extra 'pending' function
		if not self.connection:
			peer = self.neighbor.peer_address
			local = self.neighbor.local_address
			md5 = self.neighbor.md5
			ttl = self.neighbor.ttl
			self.connection = Outgoing(peer.afi,peer.ip,local.ip,self.port,md5,ttl)

			try:
				generator = self.connection.establish()
				while True:
					connected = generator.next()
					if not connected:
						yield False
						continue
					if self.peer.neighbor.api['neighbor-changes']:
						self.peer.reactor.processes.connected(self.peer.neighbor)
					yield True
					return
			except StopIteration:
				# close called by the caller
				# self.close('could not connect to remote end')
				yield False
				return

	def close (self, reason='protocol closed, reason unspecified'):
		if self.connection:
			self.logger.network(self.me(reason))

			# must be first otherwise we could have a loop caused by the raise in the below
			self.connection.close()
			self.connection = None

			try:
				if self.peer.neighbor.api['neighbor-changes']:
					self.peer.reactor.processes.down(self.peer.neighbor,reason)
			except ProcessError:
				self.logger.message(self.me('could not send notification of neighbor close to API'))

	def _to_api (self,direction,message,raw):
		packets = self.neighbor.api['%s-packets' % direction]
		parsed = self.neighbor.api['%s-parsed' % direction]
		consolidate = self.neighbor.api['%s-consolidate' % direction]

		if consolidate:
			if packets:
				self.peer.reactor.processes.message(self.peer.neighbor,direction,message,raw[:19],raw[19:])
			else:
				self.peer.reactor.processes.message(self.peer.neighbor,direction,message,'','')
		else:
			if packets:
				self.peer.reactor.processes.packets(self.peer.neighbor,direction,int(message.ID),raw[:19],raw[19:])
			if parsed:
				self.peer.reactor.processes.message(message.ID,self.peer.neighbor,direction,message,'','')

	def write (self, message, negotiated=None):
		raw = message.message(negotiated)

		if self.neighbor.api.get('send-%d' % message.ID,False):
			self._to_api('send',message,raw)

		for boolean in self.connection.writer(raw):
			yield boolean

	def send (self,raw):
		if self.neighbor.api.get('send-%d' % ord(raw[19]),False):
			message = Update.unpack_message(raw[19:],self.negotiated)
			self._to_api('send',message,raw)

		for boolean in self.connection.writer(raw):
			yield boolean

	# Read from network .......................................................

	def read_message (self):
		# This will always be defined by the loop but scope leaking upset scrutinizer/pylint
		msg_id = None

		packets = self.neighbor.api['receive-packets']
		consolidate = self.neighbor.api['receive-consolidate']
		parsed = self.neighbor.api['receive-parsed']

		body,header = '',''  # just because pylint/pylama are getting more clever

		for length,msg_id,header,body,notify in self.connection.reader():
			if notify:
				if self.neighbor.api['receive-%d' % Message.CODE.NOTIFICATION]:
					if packets and not consolidate:
						self.peer.reactor.processes.packets(self.peer.neighbor,'receive',msg_id,header,body)

					if not packets or consolidate:
						header = ''
						body = ''

					self.peer.reactor.processes.notification(self.peer.neighbor,'receive',notify.code,notify.subcode,str(notify),header,body)
				# XXX: is notify not already Notify class ?
				raise Notify(notify.code,notify.subcode,str(notify))
			if not length:
				yield _NOP

		if packets and not consolidate:
			self.peer.reactor.processes.packets(self.peer.neighbor,'receive',msg_id,header,body)

		if msg_id == Message.CODE.UPDATE:
			if not parsed and not self.log_routes:
				yield _UPDATE
				return

		self.logger.message(self.me('<< %s' % Message.CODE.name(msg_id)))
		try:
			message = Message.unpack(msg_id,body,self.negotiated)
		except (KeyboardInterrupt,SystemExit,Notify):
			raise
		except Exception,exc:
			self.logger.message(self.me('Could not decode message "%d"' % msg_id))
			self.logger.message(self.me('%s' % str(exc)))
			self.logger.message(traceback.format_exc())
			raise Notify(1,0,'can not decode update message of type "%d"' % msg_id)
			# raise Notify(5,0,'unknown message received')

		if self.neighbor.api.get('receive-%d' % msg_id,False):
			if parsed:
				if not consolidate or not packets:
					header = ''
					body = ''
				self.peer.reactor.processes.message(msg_id,self.neighbor,'receive',message,header,body)

		if message.TYPE == Notification.TYPE:
			raise message

		yield message

		# elif msg == Message.CODE.ROUTE_REFRESH:
		# 	if self.negotiated.refresh != REFRESH.ABSENT:
		# 		self.logger.message(self.me('<< ROUTE-REFRESH'))
		# 		refresh = RouteRefresh.unpack_message(body,self.negotiated)
		# 		if self.neighbor.api.receive_refresh:
		# 			if refresh.reserved in (RouteRefresh.start,RouteRefresh.end):
		# 				if self.neighbor.api.consolidate:
		# 					self.peer.reactor.process.refresh(self.peer,refresh,header,body)
		# 				else:
		# 					self.peer.reactor.processes.refresh(self.peer,refresh,'','')
		# 	else:
		# 		# XXX: FIXME: really should raise, we are too nice
		# 		self.logger.message(self.me('<< NOP (un-negotiated type %d)' % msg))
		# 		refresh = UnknownMessage.unpack_message(body,self.negotiated)
		# 	yield refresh

	def validate_open (self):
		error = self.negotiated.validate(self.neighbor)
		if error is not None:
			raise Notify(*error)

	def read_open (self, ip):
		for received_open in self.read_message():
			if received_open.TYPE == NOP.TYPE:
				yield received_open
			else:
				break

		if received_open.TYPE != Open.TYPE:
			raise Notify(5,1,'The first packet recevied is not an open message (%s)' % received_open)

		self.logger.message(self.me('<< %s' % received_open))
		yield received_open

	def read_keepalive (self):
		for message in self.read_message():
			if message.TYPE == NOP.TYPE:
				yield message
			else:
				break

		if message.TYPE != KeepAlive.TYPE:
			raise Notify(5,2)

		yield message

	#
	# Sending message to peer
	#

	def new_open (self, restarted):
		sent_open = Open(
			Version(4),
			self.neighbor.local_as,
			self.neighbor.hold_time,
			self.neighbor.router_id,
			Capabilities().new(self.neighbor,restarted)
		)

		# we do not buffer open message in purpose
		for _ in self.write(sent_open):
			yield _NOP

		self.logger.message(self.me('>> %s' % sent_open))
		yield sent_open

	def new_keepalive (self, comment=''):
		keepalive = KeepAlive()

		for _ in self.write(keepalive):
			yield _NOP

		self.logger.message(self.me('>> KEEPALIVE%s' % (' (%s)' % comment if comment else '')))

		yield keepalive

	def new_notification (self, notification):
		for _ in self.write(notification):
			yield _NOP
		self.logger.message(self.me('>> NOTIFICATION (%d,%d,"%s")' % (notification.code,notification.subcode,notification.data)))
		yield notification

	def new_update (self):
		updates = self.neighbor.rib.outgoing.updates(self.neighbor.group_updates)
		number = 0
		for update in updates:
			for message in update.messages(self.negotiated):
				number += 1
				for boolean in self.send(message):
					# boolean is a transient network error we already announced
					yield _NOP
		if number:
			self.logger.message(self.me('>> %d UPDATE(s)' % number))
		yield _UPDATE

	def new_eor (self, afi, safi):
		eor = EOR(afi,safi)
		for _ in self.write(eor):
			yield _NOP
		self.logger.message(self.me('>> EOR %s %s' % (afi,safi)))
		yield eor

	def new_eors (self, afi=AFI.undefined,safi=SAFI.undefined):
		# Send EOR to let our peer know he can perform a RIB update
		if self.negotiated.families:
			families = self.negotiated.families if (afi,safi) == (AFI.undefined,SAFI.undefined) else [(afi,safi),]
			for eor_afi,eor_safi in families:
				for _ in self.new_eor(eor_afi,eor_safi):
					yield _
		else:
			# If we are not sending an EOR, send a keepalive as soon as when finished
			# So the other routers knows that we have no (more) routes to send ...
			# (is that behaviour documented somewhere ??)
			for eor in self.new_keepalive('EOR'):
				yield _NOP
			yield _UPDATE

	def new_operational (self, operational, negotiated):
		for _ in self.write(operational,negotiated):
			yield _NOP
		self.logger.message(self.me('>> OPERATIONAL %s' % str(operational)))
		yield operational

	def new_refresh (self, refresh):
		for _ in self.write(refresh,None):
			yield _NOP
		self.logger.message(self.me('>> REFRESH %s' % str(refresh)))
		yield refresh
