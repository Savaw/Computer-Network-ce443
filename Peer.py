from Packet import Packet, PacketType
import Chatroom
import re
import socket
import threading
import random

from commons import dprint, BaseSenderReceiver


ADMIN_HOST = '127.0.0.1'
ADMIN_PORT = 23000

PEER_HOST = '127.0.0.1'

class Peer(BaseSenderReceiver):
	def __init__(self, admin_host, admin_port, peer_host):
		BaseSenderReceiver.__init__(self)
		self.admin_host = admin_host
		self.admin_port = admin_port

		self.host = peer_host
		self.listening_port = 0

		self.id = None

		self.parent_id = None
		self.parent_port = None

		self.known_peers = {}  # dict of {peer_id: peer_port}. peer_port is None for all except children
		self.children_subtree = {}  # dict of {child_id: list of peer_id}

		self.sending_socket = None
		
		self.current_chatroom: Chatroom.Chatroom = None  # So that when in a chatroom, we ignore other chatroom join requests, etc
		self.chat_disabled = False

		self.wait_for_YN = 0
		self.wait_for_chat_name = 0
		self.pending_chat_requests = []  # list of chat requests

		self.firewall = []


	def firewall_check(self, packet: Packet, flag):  # flag_send = True, flag_receive = False
		typ, id_src, id_dst = packet.type, packet.source, packet.destination
		for rule in self.firewall:
			dprint(f"checking rule {rule} ...", level=3)

			rule_direction = rule[0]
			rule_source = rule[1]
			rule_destination = rule[2]
			rule_packet_type = rule[3]
			rule_action = rule[4]

			if rule_packet_type == typ and rule_action == "ACCEPT":
				if rule_direction == 'INPUT' and (rule_source == id_src or rule_source == '*') and (id_dst == self.id or id_dst == '-1'):
					dprint(f"Your input packet is accepted in match with {rule} rule.", level=2)
					return True
				elif rule_direction == 'OUTPUT' and id_src == self.id and (rule_destination == id_dst or id_dst == '-1' or rule_destination == '*'):
					dprint(f"Your output packet is accepted in match with {rule} rule.", level=2)
					return True
				elif rule_direction == 'FORWARD' and (rule_source == id_src or rule_source == '*') and (rule_destination == id_dst or id_dst == '-1' or rule_destination == '*'):
					if flag:
						dprint(f"Your forward packet is accepted in match with {rule} rule.", level=2)
						return True
			elif rule_packet_type == typ and rule_action == "DROP":
				if rule_direction == 'INPUT' and (rule_source == id_src or rule_source == '*') and (id_dst == self.id or id_dst == '-1'):
					dprint(f"Your input packet is dropped in match with {rule} rule.", level=2)
					return False
				elif rule_direction == 'OUTPUT' and id_src == self.id and (rule_destination == id_dst or id_dst == '-1' or rule_destination == '*'):
					dprint(f"Your output packet is dropped in match with {rule} rule.", level=2)
					return False
				elif rule_direction == 'FORWARD' and (rule_source == id_src or rule_source == '*') and (rule_destination == id_dst or id_dst == '-1' or rule_destination == '*'):
					if flag:
						dprint(f"Your forward packet is dropped in match with {rule} rule.", level=2)
						return False
		return True


	def add_to_known_peers(self, id_, port=None):
		if id_ not in self.known_peers or self.known_peers[id_] == None:
			dprint(f'add peer with id {id_} and port {port} to known peers', level=2)
			self.known_peers[id_] = port

	def send_packet_to_peer(self, peer_port, packet):
		try:
			if self.firewall_check(packet, flag=True):
				self.send_packet(self.sending_socket, packet, (self.host, peer_port))
		except OSError as e:
			dprint(f"could not send packet: {packet} to port {peer_port}, err: {e}")
			return False
		return True
	
	def receive_packet_from_server(self, server):
		while True:
			pkt = self.receive_packet_udp(server)
			if pkt:
				packet, peer_address = pkt
				peer_port = peer_address[1]
				if self.firewall_check(packet, flag=False):
					return packet, peer_port
			else:
				continue

	def advertise_to_parent(self, peer_id):
		if not self.parent_port:
			return
		packet = Packet(PacketType.PARENT_ADVERTISE, self.id, self.parent_id, peer_id)
		self.send_packet_to_peer(self.parent_port, packet)

	def add_new_child(self, id_):
		dprint(f'add new child with id {id_}', level=2)
		self.children_subtree[id_] = []
		self.add_to_child_subtree(id_, id_)

	def add_to_child_subtree(self, new_peer_id, child_id):
		dprint(f'add new peer with id {new_peer_id} to child subtree with id {child_id}', level=2)
		if child_id not in self.children_subtree:
			dprint(f'child {child_id} is not in child_subtree dict', level=2)
			return

		self.children_subtree[child_id].append(new_peer_id)

	def get_sending_port_from_listening_port(self, listening_port):
		return listening_port + 1

	def get_listen_port_from_sending_port(self, sending_port):
		return sending_port - 1

	def are_ports_for_same_peer(self, listen_port, sending_port):
		return self.get_listen_port_from_sending_port(sending_port) == listen_port

	def send_packet_to_all(self, packet: Packet, sender_port=None):
		for child in self.children_subtree.keys():
			if not self.are_ports_for_same_peer(self.known_peers[child], sender_port):
				dprint(
					f'route packet with source {packet.source} and destination {packet.destination} to CHILD {child} with port {self.known_peers[child]}',
					level=3)
				self.send_packet_to_peer(self.known_peers[child], packet)

		if self.parent_port:
			if not self.are_ports_for_same_peer(self.parent_port, sender_port):
				dprint(
					f'route packet with source {packet.source} and destination {packet.destination} to PARENT {self.parent_id} with port {self.parent_port}',
					level=3)
				self.send_packet_to_peer(self.parent_port, packet)

	def route_packet(self, packet: Packet, sender_port=None):
		if not sender_port:
			sender_port = self.get_sending_port_from_listening_port(self.listening_port)

			if packet.destination != '-1' and packet.destination not in self.known_peers:
				print(f'Unknown destination {packet.destination}')
				return False

		if packet.destination == '-1':
			self.send_packet_to_all(packet, sender_port)
			return True

		for child in self.children_subtree.keys():
			if packet.destination in self.children_subtree[child]:
				dprint(
					f'route packet with source {packet.source} and destination {packet.destination} to CHILD {child} with port {self.known_peers[child]}',
					level=3)
				self.send_packet_to_peer(self.known_peers[child], packet)
				return True

		if self.parent_port:
			dprint(
				f'route packet with source {packet.source} and destination {packet.destination} to PARENT {self.parent_id} with port {self.parent_port}',
				level=3)
			self.send_packet_to_peer(self.parent_port, packet)
			return True

		if self.id != packet.source:
			not_found_packet = Packet(PacketType.DESTINATION_NOT_FOUND, self.id, packet.source,
									  f"DESTINATION {packet.destination} NOT FOUND")
			self.send_packet_to_peer(self.get_listen_port_from_sending_port(sender_port), not_found_packet)

		dprint(f'could not route packet with source {packet.source} and destination {packet.destination}', level=3)
		return False

	def peer_receiving_handler(self, server):
		''' Receive messages from peers '''
		while True:
			try:
				packet, peer_port = self.receive_packet_from_server(server)

				# If packet is routing response we need to change it before continue
				if packet.type == PacketType.ROUTING_RESPONSE:
					if self.are_ports_for_same_peer(self.parent_port, peer_port):
						new_data = f'{self.id} <- {packet.data}'
					else:
						new_data = f'{self.id} -> {packet.data}'
					packet = Packet(PacketType.ROUTING_RESPONSE, packet.source, packet.destination, new_data)

				# If packet dest is not only us we need to route it
				if packet.destination != self.id:
					self.route_packet(packet, peer_port)
					# If we aren't included in packet dist we pass
					if packet.destination != '-1':
						print(f"{packet.type.code} Packet from {packet.source} to {packet.destination}")
						continue

				self.add_to_known_peers(packet.source)

				# If we reach here it means packet is for us
				if packet.type == PacketType.CONNECTION_REQUEST:
					peer_port = int(packet.data)
					self.add_to_known_peers(packet.source, peer_port)
					self.advertise_to_parent(packet.source)
					self.add_new_child(packet.source)

				elif packet.type == PacketType.PARENT_ADVERTISE:
					peer_id = packet.data
					self.add_to_known_peers(peer_id)
					self.advertise_to_parent(peer_id)
					self.add_to_child_subtree(peer_id, packet.source)

				elif packet.type == PacketType.ADVERTISE:
					peer_id = packet.data
					self.add_to_known_peers(peer_id)
					self.add_to_child_subtree(peer_id, packet.source)

				elif packet.type == PacketType.ROUTING_REQUEST:
					response_packet = Packet(PacketType.ROUTING_RESPONSE, self.id, packet.source, self.id)
					self.route_packet(response_packet, peer_port)

				elif packet.type == PacketType.ROUTING_RESPONSE and self.current_chatroom == None:
					print(packet.data)

				elif packet.type == PacketType.DESTINATION_NOT_FOUND and self.current_chatroom == None:
					print(packet.data)

				elif packet.type == PacketType.MESSAGE:
					# handling Salam message
					if packet.data.startswith('SALAM:') and self.current_chatroom == None:
						hello_msg = packet.data.removeprefix('SALAM:').strip()
						if re.fullmatch('Salam Salam Sad Ta Salam', hello_msg, flags=re.IGNORECASE):
							response_msg = "Hezaro Sisad Ta Salam"
							response_packet = Packet(PacketType.MESSAGE, self.id, packet.source,
													 f'SALAM:{response_msg}')
							print(f"{hello_msg} ({packet.source})")
							self.route_packet(response_packet, peer_port)

						elif re.fullmatch('Hezaro Sisad Ta Salam', hello_msg, flags=re.IGNORECASE):
							print(f"{hello_msg} ({packet.source})")

					# Chat messages
					elif packet.data.startswith('CHAT:') and not self.chat_disabled:

						chat_msg = packet.data.removeprefix('CHAT:').strip()

						dprint(
							f"recieved chat message {chat_msg} - currectly have chatroom: {self.current_chatroom != None}",
							level=3)

						# If we are not in chatroom
						if self.current_chatroom is None:
							dprint(f"we are not in chatroom {chat_msg}", level=2)
							if re.match('^REQUESTS FOR STARTING CHAT WITH', chat_msg,
											flags=re.IGNORECASE):
								chatname_invitor = chat_msg.split(": ")[0].split()[-1]
								id_invitor = chat_msg.split(": ")[1].split(", ")[0]
								members = chat_msg.splitlines()[0].split(": ")[1].split(", ")
								chat_id = int(chat_msg.splitlines()[1])

								print(
									f"{chatname_invitor} with id {id_invitor} has asked you to join a chat. Would you like to join?[Y/N]")
								dprint(f"members {members}", level=3)

								self.wait_for_YN += 1
								self.pending_chat_requests.append((chatname_invitor, id_invitor, members, chat_id))

						# If we are in chatroom
						else:
							chat_id_ = int(chat_msg.splitlines()[1])
							if self.current_chatroom.chat_id == chat_id_:
								if re.match('^JOIN:', chat_msg, flags=re.IGNORECASE):
									chat_msg = chat_msg.removeprefix('JOIN:')
									splited = chat_msg.splitlines()[0].split(':')
									id_ = splited[0]
									chat_name = splited[1]

									self.current_chatroom.add_member(id_, chat_name)
									print(f'{chat_name}({id_}) was joind to the chat.')

									response_msg = f"CHAT:METOO:{self.id}:{self.current_chatroom.my_name}\n{self.current_chatroom.chat_id}"
									packet = Packet(PacketType.MESSAGE, self.id, id_, response_msg)
									self.route_packet(packet)

								elif re.match('^METOO:', chat_msg, flags=re.IGNORECASE):
									chat_msg = chat_msg.removeprefix("METOO:")
									splited = chat_msg.splitlines()[0].split(':')
									id_ = splited[0]
									chat_name = splited[1]
									self.current_chatroom.add_member(id_, chat_name)

								elif re.match('^NEW:', chat_msg, flags=re.IGNORECASE):
									peer_id = packet.source
									peer_chat_name = self.current_chatroom.get_peer_chatname(peer_id)
									new_chat = chat_msg.removeprefix('NEW:')
									print(f"{peer_chat_name}: {new_chat.splitlines()[0]}")

								elif re.match('^EXIT CHAT', chat_msg, flags=re.IGNORECASE):
									exited_peer_id = chat_msg.split()[2]
									exited_peer_name = self.current_chatroom.get_peer_chatname(exited_peer_id)

									self.current_chatroom.remove_member(exited_peer_id)
									print(f"{exited_peer_name}({exited_peer_id}) left the chat.")


			except OSError as e:
				dprint(f"Error", e)
				pass

	def input_handler(self):
		''' Get inputs from terminal and send messages '''
		while True:
			msg = input()

			# Check if any chat request is pending
			if self.wait_for_YN > 0:
				self.wait_for_YN -= 1

				chatname_invitor, id_invitor, members, chat_id = self.pending_chat_requests[0]
				self.pending_chat_requests = self.pending_chat_requests[1:]

				answer = msg
				if answer == "Y":
					print("Choose a name for yourself")
					chat_name = input()
					response_message = f"CHAT:JOIN:{self.id}:{chat_name}\n{chat_id}"

					self.current_chatroom = Chatroom.Chatroom(chat_name, chat_id)
					self.current_chatroom.add_member(id_invitor, chatname_invitor)

					for member_id in members:
						if member_id != self.id:
							self.add_to_known_peers(member_id)
							self.current_chatroom.add_member(member_id)

							packet = Packet(PacketType.MESSAGE, self.id, member_id, response_message)
							self.route_packet(packet)

					self.pending_chat_requests = []

			elif self.current_chatroom is None:
				if re.fullmatch('SHOW KNOWN CLIENTS', msg, flags=re.IGNORECASE):
					for p in self.known_peers.keys():
						print(p)

				elif re.fullmatch('ROUTE (\w+)', msg, flags=re.IGNORECASE):
					dest_id = msg.split()[1]

					if dest_id == self.id:
						print(self.id)
						continue

					packet = Packet(PacketType.ROUTING_REQUEST, self.id, dest_id, '')
					self.route_packet(packet)

				elif re.fullmatch('ADVERTISE (-?\w+)', msg, flags=re.IGNORECASE):
					dest_id = msg.split()[1]
					packet = Packet(PacketType.ADVERTISE, self.id, dest_id, self.id)
					self.route_packet(packet)

				elif re.fullmatch('FIlTER (INPUT|OUTPUT|FORWARD) (\w+|[*]) (\w+|[*]) (\w+) (ACCEPT|DROP)', msg,
								  flags=re.IGNORECASE):
					direction, id_src, id_dst, typ, action = msg.split()[1:]
					self.firewall.insert(0, (direction, id_src, id_dst, PacketType.get_packet_type_from_code(typ), action))

				elif re.fullmatch('FW CHAT (ACCEPT|DROP)', msg, flags=re.IGNORECASE):
					action = msg.split()[-1]
					if action == "ACCEPT":
						self.chat_disabled = False
					elif action == "DROP":
						self.chat_disabled = True

				elif re.fullmatch('Salam Salam Sad Ta Salam (-?\w+)', msg, flags=re.IGNORECASE):
					dest_id = msg.split()[-1]
					packet = Packet(PacketType.MESSAGE, self.id, dest_id, 'SALAM:Salam Salam Sad Ta Salam')
					self.route_packet(packet)

				elif re.fullmatch('START CHAT (\w+):( ((\w+), )*(\w+))?', msg, flags=re.IGNORECASE):
					if self.chat_disabled:
						print('Chat is disabled. Make sure the firewall allows you to chat.')
						continue

					random.seed()
					chat_id = random.randint(1, 1000000)
					splited = msg.split(": ")
					chat_name = splited[0].split()[2]
					possible_members = splited[1].split(", ")

					self.current_chatroom = Chatroom.Chatroom(chat_name, chat_id)

					for member in possible_members:
						if member in self.known_peers:
							self.current_chatroom.add_member(member)

					member_list_msg = self.id
					for member in self.current_chatroom.members:
						if member != self.id:
							member_list_msg += ", "
							member_list_msg += member

					request_msg = f"CHAT:REQUESTS FOR STARTING CHAT WITH {chat_name}: {member_list_msg}\n{chat_id}"

					for member in self.current_chatroom.members:
						if member != self.id:
							packet = Packet(PacketType.MESSAGE, self.id, member, request_msg)
							self.route_packet(packet)

				else:
					print("INVALID COMMAND")

			# In Chatroom
			else:
				if re.fullmatch('EXIT CHAT', msg, flags=re.IGNORECASE):
					packet_data = f"CHAT:EXIT CHAT {self.id}\n{self.current_chatroom.chat_id}"
					for member in self.current_chatroom.get_definite_members():
						packet = Packet(PacketType.MESSAGE, self.id, member, packet_data)
						self.route_packet(packet)

					self.current_chatroom = None

				else:
					packet_data = f"CHAT:NEW:{msg}\n{self.current_chatroom.chat_id}"
					for member in self.current_chatroom.get_definite_members():
						packet = Packet(PacketType.MESSAGE, self.id, member, packet_data)
						self.route_packet(packet)


	def init_sender(self):
		sending_port = self.get_sending_port_from_listening_port(self.listening_port)
		try:
			self.sending_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # UDP
			self.sending_socket.bind((self.host, sending_port))
		except OSError as e:
			print(f"ERROR: could not bind sending socket to {sending_port}")
			return False

		return True

	def start(self):
		''' Get inputs from terminal and send messages '''
		while True:
			start_msg = input()
			if re.fullmatch('CONNECT AS (\w+) ON PORT (\d+)', start_msg, flags=re.IGNORECASE):
				start_msg_arr = start_msg.split()
				self.id, self.listening_port = start_msg_arr[2], int(start_msg_arr[-1])
				self.add_to_known_peers(self.id, self.listening_port)

				try:
					server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # UDP
					server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
					server.bind((self.host, self.listening_port))  # passing zero will choose a random free port
				except OSError:
					print(f"ERROR: could not bind listening socket to {self.host} {self.listening_port}")
					continue

				# connect to admin to get parent in network
				with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:  # TCP
					peer.connect((self.admin_host, self.admin_port))
					dprint(f"Peer is connected to admin {self.admin_host}:{self.admin_port}")

					conn_msg = f"{self.id} REQUESTS FOR CONNECTING TO NETWORK ON PORT {self.listening_port}"
					self.send(peer, conn_msg)

					msg = self.receive(peer)
					if re.fullmatch('CONNECT TO (-?\w+) WITH PORT (-?\d+)', msg, flags=re.IGNORECASE):
						msg_splited = msg.split()
						self.parent_id = msg_splited[2] if msg_splited[2] != '-1' else None
						self.parent_port = int(msg_splited[-1]) if msg_splited[2] != '-1' else None
					else:
						print(msg)
						continue

				# start listening for incoming messages from peers
				thread = threading.Thread(target=self.peer_receiving_handler, args=[server])
				thread.start()

				# init sending socket
				if not self.init_sender():
					continue

				# connect to parent and send listening port to parent if it is not root
				if self.parent_id:
					self.add_to_known_peers(self.parent_id, self.parent_port)
					packet = Packet(PacketType.CONNECTION_REQUEST, self.id, self.parent_id, self.listening_port)
					self.send_packet_to_peer(self.parent_port, packet)

				dprint('successfully connected to network')

				# start listening for commands
				self.input_handler()

			else:
				print("INVALID COMMAND")


if __name__ == "__main__":
	client = Peer(ADMIN_HOST, ADMIN_PORT, PEER_HOST)
	client.start()
