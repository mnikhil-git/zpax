import json

from zpax import tzmq

from paxos import multi, basic
from paxos.proposers import heartbeat

from twisted.internet import defer, task, reactor


class ProposalFailed(Exception):
    pass


class SequenceMismatch(ProposalFailed):

    def __init__(self, current):
        super(SequenceMismatch,self).__init__('Sequence Number Mismatch')
        self.current_seq_num = current

        
class ValueAlreadyProposed(ProposalFailed):
    
    def __init__(self):
        super(ValueAlreadyProposed,self).__init__('Value Already Proposed')


class BasicHeartbeatProposer (heartbeat.Proposer):
    hb_period       = 0.5
    liveness_window = 1.5

    def __init__(self, basic_node, node_uid, quorum_size, leader_uid):
        self.node = basic_node

        super(BasicHeartbeatProposer, self).__init__(node_uid,
                                                     quorum_size,
                                                     leader_uid = leader_uid)

    def send_prepare(self, proposal_id):
        self.node._paxos_send_prepare(proposal_id)

    def send_accept(self, proposal_id, proposal_value):
        self.node._paxos_send_accept(proposal_id, proposal_value)

    def send_heartbeat(self, leader_proposal_id):
        self.node._paxos_send_heartbeat(leader_proposal_id)

    def schedule(self, msec_delay, func_obj):
        pass

    def on_leadership_acquired(self):
        self.node._paxos_on_leadership_acquired()

    def on_leadership_lost(self):
        self.node._paxos_on_leadership_lost()

    def on_leadership_change(self, prev_leader_uid, new_leader_uid):
        self.node._paxos_on_leadership_change(prev_leader_uid, new_leader_uid)
        


class BasicMultiPaxos(multi.MultiPaxos):

    def __init__(self, node_uid, quorum_size, sequence_number, node_factory,
                 on_resolution_callback):

        self.node_factory     = node_factory
        self.on_resolution_cb = on_resolution_callback
        
        super(BasicMultiPaxos, self).__init__( node_uid,
                                                quorum_size,
                                                sequence_number )
                
    def on_proposal_resolution(self, instance_num, value):
        self.on_resolution_cb(instance_num, value)
    



class BasicNode (object):
    '''
    This class provides the basic functionality required for Multi-Paxos
    over ZeroMQ Publish/Subscribe sockets. This class follows the GoF95
    template design pattern and delegates all application level logic to
    a subclass.
    '''

    hb_proposer_klass = BasicHeartbeatProposer

    def __init__(self, node_uid,
                 local_pub_sub_addr,
                 remote_pub_sub_addrs,
                 quorum_size,
                 sequence_number=0):

        self.node_uid         = node_uid
        self.local_ps_addr    = local_pub_sub_addr
        self.remote_ps_addrs  = remote_pub_sub_addrs
        self.quorum_size      = quorum_size
        self.sequence_number  = sequence_number
        self.accept_retry     = None

        self.mpax             = BasicMultiPaxos(node_uid,
                                                quorum_size,
                                                sequence_number,
                                                self._node_factory,
                                                self._on_proposal_resolution)
        
        self.heartbeat_poller = task.LoopingCall( self._poll_heartbeat         )
        self.heartbeat_pulser = task.LoopingCall( self._pulse_leader_heartbeat )
        
        self.pub              = tzmq.ZmqPubSocket()
        self.sub              = tzmq.ZmqSubSocket()

        self.sub.subscribe = 'zpax'
        
        self.sub.messageReceived    = self._on_sub_received
        
        self.pub.bind(self.local_ps_addr)

        for x in remote_pub_sub_addrs:
            self.sub.connect(x)

        self.heartbeat_poller.start( self.hb_proposer_klass.liveness_window )

    
    #--------------------------------------------------------------------------
    # Subclass API
    #
    def onLeadershipAcquired(self):
        '''
        Called when this node acquires Paxos leadership
        '''

    def onLeadershipLost(self):
        '''
        Called when this node looses Paxos leadership
        '''

    def onLeadershipChanged(self, prev_leader_uid, new_leader_uid):
        '''
        Called whenver Paxos leadership changes.
        '''

    def onBehindInSequence(self):
        '''
        Called when this node's sequence number is behind the current value
        '''

    def onOtherNodeBehindInSequence(self, node_uid):
        '''
        Called when a request from another node on the network is using an out-of-date
        sequence number
        '''

    def onProposalResolution(self, instance_num, value):
        '''
        Called when an instance of the Paxos algorithm agrees on a value
        '''

    def onHeartbeat(self, data):
        '''
        data - Dictionary of key=value paris in the heartbeat message
        '''

    def onShutdown(self):
        '''
        Called immediately before shutting down
        '''

    def getHeartbeatData(self):
        '''
        Returns a dictionary of key=value parameters to be included
        in the heartbeat message
        '''
        return {}
    
    def slewSequenceNumber(self, new_sequence_number):
        assert new_sequence_number > self.sequence_number
        
        self.sequence_number = new_sequence_number
        
        if self.mpax.node.proposer.leader:
            self.paxos_on_leadership_lost()
            
        self.mpax.set_instance_number(self.sequence_number)

        
    def proposeValue(self, sequence_number, value):
        if not sequence_number == self.sequence_number:
            raise SequenceMismatch( self.sequence_number )

        if self.mpax.node.proposer.value is not None:
            raise ValueAlreadyProposed()

        if self.mpax.node.acceptor.accepted_value is not None:
            raise ValueAlreadyProposed()
        
        self.publish( 'value_proposal', dict(value=value) )
        self.mpax.set_proposal(self.sequence_number, value)


    def publish(self, message_type, *parts):
        if not parts:
            parts = [{}]
            
        parts[0]['type'    ] = message_type
        parts[0]['node_uid'] = self.node_uid
        parts[0]['seq_num' ] = self.sequence_number
        
        msg_stack = [ 'zpax' ]

        msg_stack.extend( json.dumps(p) for p in parts )
        
        self.pub.send( msg_stack )
        self._on_sub_received( msg_stack )


    def shutdown(self):
        self.onShutdown()
        if self.accept_retry is not None and self.accept_retry.active():
            self.accept_retry.cancel()
        self.pub.close()
        self.sub.close()
        if self.heartbeat_poller.running:
            self.heartbeat_poller.stop()
        if self.heartbeat_pulser.running:
            self.heartbeat_pulser.stop()
            
    #--------------------------------------------------------------------------
    # Helper Methods
    #
    def _node_factory(self, node_uid, leader_uid, quorum_size, resolution_callback):
        return basic.Node( self.hb_proposer_klass(self, node_uid, quorum_size, leader_uid),
                           basic.Acceptor(),
                           basic.Learner(quorum_size),
                           resolution_callback )

            
    #--------------------------------------------------------------------------
    # Heartbeats 
    #
    def _poll_heartbeat(self):
        self.mpax.node.proposer.poll_liveness()

        
    def _pulse_leader_heartbeat(self):
        self.mpax.node.proposer.pulse()

        
    #--------------------------------------------------------------------------
    # Paxos Leadership Changes 
    #
    def _paxos_on_leadership_acquired(self):
        self.heartbeat_pulser.start( self.hb_proposer_klass.hb_period )
        self.onLeadershipAcquired()

        
    def _paxos_on_leadership_lost(self):
        if self.accept_retry is not None:
            self.accept_retry.cancel()
            self.accept_retry = None
            
        if self.heartbeat_pulser.running:
            self.heartbeat_pulser.stop()

        self.onLeadershipLost()


    def _paxos_on_leadership_change(self, prev_leader_uid, new_leader_uid):
        self.onLeadershipChanged(prev_leader_uid, new_leader_uid)

        
    #--------------------------------------------------------------------------
    # Paxos Messaging 
    #
    def _on_sub_received(self, msg_parts):
        '''
        msg_parts - [0] 'zpax'
                    [1] BasicNode's JSON-encoded message content 
                    [2] If present, it's a JSON-encoded Paxos message
        '''
        try:
            parts = [ json.loads(p) for p in msg_parts[1:] ]
        except ValueError:
            print 'Invalid JSON: ', msg_parts
            return

        if not 'type' in parts[0]:
            print 'Missing message type'
            return

        fobj = getattr(self, '_on_sub_' + parts[0]['type'], None)
        
        if fobj:
            fobj(*parts)


    def _check_sequence(self, header):
        seq = header['seq_num']
        
        if seq > self.sequence_number:
            self.onBehindInSequence()
            
        elif seq < self.sequence_number:
            self.onOtherNodeBehindInSequence(header['node_uid'])

        return seq == self.sequence_number


    def _on_sub_paxos_heartbeat(self, header, pax):
        self.mpax.node.proposer.recv_heartbeat( tuple(pax[0]) )
        self.onHeartbeat( header )

    
    def _on_sub_paxos_prepare(self, header, pax):
        #print self.node_uid, 'got prepare', header, pax
        if self._check_sequence(header):
            r = self.mpax.recv_prepare(header['seq_num'], tuple(pax[0]))
            if r:
                #print self.node_uid, 'sending promise'
                self.publish( 'paxos_promise', {}, r )

            
    def _on_sub_paxos_promise(self, header, pax):
        #print self.node_uid, 'got promise', header, pax
        if self._check_sequence(header):
            r = self.mpax.recv_promise(header['seq_num'],
                                       header['node_uid'],
                                       tuple(pax[0]),
                                       tuple(pax[1]) if pax[1] else None, pax[2])
            if r and r[1] is not None:
                #print self.node_uid, 'sending accept', r
                self._paxos_send_accept( *r )
            

    def _on_sub_paxos_accept(self, header, pax):
        #print 'Got Accept!', pax
        if self._check_sequence(header):
            r = self.mpax.recv_accept_request(header['seq_num'],
                                              tuple(pax[0]),
                                              pax[1])
            if r:
                self.publish( 'paxos_accepted', {}, r )


    def _on_sub_paxos_accepted(self, header, pax):
        #print 'Got accepted', header, pax
        if self._check_sequence(header):
            self.mpax.recv_accepted(header['seq_num'], header['node_uid'],
                                    tuple(pax[0]), pax[1])
        

    def _paxos_send_prepare(self, proposal_id):
        #print self.node_uid, 'sending prepare: ', proposal_id
        self.publish( 'paxos_prepare', {}, [proposal_id,] )

        
    def _paxos_send_accept(self, proposal_id, proposal_value):
        if self.mpax.have_leadership() and (
            self.accept_retry is None or not self.accept_retry.active()
            ):
            #print 'Sending accept'
            self.publish( 'paxos_accept', {}, [proposal_id, proposal_value] )

            retry_delay = self.mpax.node.proposer.hb_period
            
            self.accept_retry = reactor.callLater(retry_delay,
                                                  self._paxos_send_accept,
                                                  proposal_id,
                                                  proposal_value)
            

    def _paxos_send_heartbeat(self, leader_proposal_id):
        self.publish( 'paxos_heartbeat', self.getHeartbeatData(), [leader_proposal_id,] )

        
    #--------------------------------------------------------------------------
    # BasicNode's Pub-Sub Messaging 
    #   
    def _on_sub_value_proposal(self, header):
        #print 'Proposal made. Seq = ', self.sequence_number, 'Req: ', header
        if header['seq_num'] == self.sequence_number:
            if self.mpax.node.acceptor.accepted_value is None:
                #print 'Setting proposal'
                self.mpax.set_proposal(self.sequence_number, header['value'])

                
    #--------------------------------------------------------------------------
    # Paxos Proposal Resolution 
    #
    def _on_proposal_resolution(self, instance_num, value):
        if self.accept_retry is not None:
            self.accept_retry.cancel()
            self.accept_retry = None
            
        self.value            = value
        self.sequence_number  = instance_num + 1

        self.onProposalResolution(instance_num, value)
    
