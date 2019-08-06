#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 by Christian Tremblay, P.Eng <christian.tremblay@servisys.com>
# Licensed under LGPLv3, see file LICENSE in this source tree.
#
"""
Discover.py

Classes needed to make discovering functions on a BACnet network

"""
# --- standard Python modules ---
import time

# --- 3rd party modules ---
from bacpypes.debugging import bacpypes_debugging
from bacpypes.apdu import WhoIsRequest, IAmRequest

from bacpypes.core import deferred
from bacpypes.pdu import Address, GlobalBroadcast, LocalBroadcast
from bacpypes.primitivedata import Unsigned
from bacpypes.constructeddata import Array
from bacpypes.object import get_object_class, get_datatype
from bacpypes.iocb import IOCB, SieveQueue, IOController

from bacpypes.netservice import NetworkServiceAccessPoint, NetworkServiceElement
from bacpypes.npdu import (
    WhoIsRouterToNetwork,
    IAmRouterToNetwork,
    InitializeRoutingTable,
    InitializeRoutingTableAck,
    WhatIsNetworkNumber,
    NetworkNumberIs,
    RejectMessageToNetwork,
)

# --- this application's modules ---
from ..io.IOExceptions import (
    SegmentationNotSupported,
    ReadPropertyException,
    ReadPropertyMultipleException,
    NoResponseFromController,
    ApplicationNotStarted,
)
from ...core.utils.notes import note_and_log

# ------------------------------------------------------------------------------
@note_and_log
class NetworkServiceElementWithRequests(IOController, NetworkServiceElement):
    """
    This class will add the capability to send requests at network level
    And capability to read responses for NPDU
    Deals with IOCB so the request can be deferred to task manager

    """

    def __init__(self):
        NetworkServiceElement.__init__(self)
        IOController.__init__(self)

        # no pending request
        self._request = None
        self._iartn = []
        self._learnedNetworks = set()
        self.queue_by_address = {}

    def process_io(self, iocb):
        # get the destination address from the pdu
        adapter, npdu = iocb.args[0]
        destination_address = npdu.pduDestination

        # look up the queue
        queue = self.queue_by_address.get(destination_address, None)
        if not queue:
            queue = SieveQueue(self.request, address=destination_address)
            self.queue_by_address[destination_address] = queue
        # ask the queue to process the request
        queue.request_io(iocb)

    def _net_complete(self, npdu):

        # look up the queue
        queue = self.queue_by_address.get(npdu.pduDestination, None)
        if not queue:
            return

        # make sure it has an active iocb
        if not queue.active_iocb:
            return

        # this request is complete
        if isinstance(
            npdu,
            (
                None.__class__,
                IAmRouterToNetwork,
                InitializeRoutingTableAck,
                NetworkNumberIs,
            ),
        ):
            queue.complete_io(queue.active_iocb, npdu)
        elif isinstance(npdu, RejectMessageToNetwork):
            queue.abort_io(queue.active_iocb, npdu)
        else:
            raise RuntimeError("unrecognized NPDU type")

        # if the queue is empty and idle, forget about the controller
        if not queue.ioQueue.queue and not queue.active_iocb:
            del self.queue_by_address[npdu.pduDestination]

    def request(self, arg):
        adapter, npdu = arg
        # save a copy of the request
        self._request = npdu

        # forward it along
        NetworkServiceElement.request(self, adapter, npdu)

    def indication(self, adapter, npdu):
        if isinstance(npdu, IAmRouterToNetwork):
            if isinstance(self._request, WhoIsRouterToNetwork):
                self._log.info(
                    "{} router to {}".format(npdu.pduSource, npdu.iartnNetworkList)
                )
                address = str(npdu.pduSource)
                self._iartn.append(address)
            for each in npdu.iartnNetworkList:
                self._learnedNetworks.add(int(each))

        elif isinstance(npdu, InitializeRoutingTableAck):
            self._log.info("{} routing table".format(npdu.pduSource))
            for rte in npdu.irtaTable:
                self._log.info(
                    "    {} {} {}".format(rte.rtDNET, rte.rtPortID, rte.rtPortInfo)
                )

        elif isinstance(npdu, NetworkNumberIs):
            self._log.info(
                "{} network number is {}".format(npdu.pduSource, npdu.nniNet)
            )
            self._learnedNetworks.add(int(npdu.nniNet))

        elif isinstance(npdu, RejectMessageToNetwork):
            self._log.warning(
                "{} Rejected message to network (reason : {})".format(
                    npdu.pduSource,
                    rejectMessageToNetworkReasons[npdu.rmtnRejectionReason],
                )
            )
        # forward it along
        NetworkServiceElement.indication(self, adapter, npdu)

    def response(self, adapter, npdu):
        # forward it along
        NetworkServiceElement.response(self, adapter, npdu)

    def confirmation(self, adapter, npdu):
        # forward it along
        self._net_complete(npdu)
        NetworkServiceElement.confirmation(self, adapter, npdu)


@note_and_log
class Discover:
    """
    Define BACnet WhoIs and IAm functions.
    """

    def whois(self, *args, global_broadcast=False):
        """
        Build a WhoIs request

        :param args: string built as [ <addr>] [ <lolimit> <hilimit> ] **optional**
        :returns: discoveredDevices as a defaultdict(int)

        Example::

            whois()             # WhoIs broadcast globally.  Every device will respond with an IAm
            whois('2:5')        # WhoIs looking for the device at (Network 2, Address 5)
            whois('10 1000')    # WhoIs looking for devices in the ID range (10 - 1000)

        """
        if not self._started:
            raise ApplicationNotStarted("BACnet stack not running - use startApp()")

        if args:
            args = args[0].split()
        msg = args if args else "any"

        self._log.debug("do_whois {!r}".format(msg))

        # build a request
        request = WhoIsRequest()
        if (len(args) == 1) or (len(args) == 3):
            request.pduDestination = Address(args[0])
            del args[0]
        else:
            if global_broadcast:
                request.pduDestination = GlobalBroadcast()
            else:
                request.pduDestination = LocalBroadcast()

        if len(args) == 2:
            try:
                request.deviceInstanceRangeLowLimit = int(args[0])
                request.deviceInstanceRangeHighLimit = int(args[1])
            except ValueError:
                pass
        self._log.debug("{:>12} {}".format("- request:", request))

        iocb = IOCB(request)  # make an IOCB
        self.this_application._last_i_am_received = []
        # pass to the BACnet stack
        deferred(self.this_application.request_io, iocb)

        iocb.wait()  # Wait for BACnet response

        if iocb.ioResponse:  # successful response
            apdu = iocb.ioResponse

        if iocb.ioError:  # unsuccessful: error/reject/abort
            pass

        time.sleep(3)
        self.discoveredDevices = self.this_application.i_am_counter
        return self.this_application._last_i_am_received

    def iam(self):
        """
        Build an IAm response.  IAm are sent in response to a WhoIs request that;
        matches our device ID, whose device range includes us, or is a broadcast.
        Content is defined by the script (deviceId, vendor, etc...)

        :returns: bool

        Example::

            iam()
        """

        self._log.debug("do_iam")

        try:
            # build a response
            request = IAmRequest()
            request.pduDestination = GlobalBroadcast()

            # fill the response with details about us (from our device object)
            request.iAmDeviceIdentifier = self.this_device.objectIdentifier
            request.maxAPDULengthAccepted = self.this_device.maxApduLengthAccepted
            request.segmentationSupported = self.this_device.segmentationSupported
            request.vendorID = self.this_device.vendorIdentifier
            self._log.debug("{:>12} {}".format("- request:", request))

            iocb = IOCB(request)  # make an IOCB
            deferred(self.this_application.request_io, iocb)
            iocb.wait()
            return True

        except Exception as error:
            self._log.error("exception: {!r}".format(error))
            return False

    def whois_router_to_network(self, args=None):
        # build a request
        try:
            request = WhoIsRouterToNetwork()
            if not args:
                request.pduDestination = LocalBroadcast()
            elif args[0].isdigit():
                request.pduDestination = LocalBroadcast()
                request.wirtnNetwork = int(args[0])
            else:
                request.pduDestination = Address(args[0])
                if len(args) > 1:
                    request.wirtnNetwork = int(args[1])
        except:
            self._log.error("WhoIsRouterToNetwork : invalid arguments")
            return
        iocb = IOCB((self.this_application.nsap.local_adapter, request))  # make an IOCB
        iocb.set_timeout(2)
        deferred(self.this_application.nse.request_io, iocb)
        iocb.wait()

        try:
            self.init_routing_table(str(self.this_application.nse._iartn.pop()))
        except IndexError:
            pass

    def init_routing_table(self, address):
        """
        irt <addr>

        Send an empty Initialize-Routing-Table message to an address, a router
        will return an acknowledgement with its routing table configuration.
        """
        # build a request
        self._log.info("Addr : ", address)
        try:
            request = InitializeRoutingTable()
            request.pduDestination = Address(address)
        except:
            self._log.error("invalid arguments")
            return

        iocb = IOCB((self.this_application.nsap.local_adapter, request))  # make an IOCB
        iocb.set_timeout(2)
        deferred(self.this_application.nse.request_io, iocb)
        iocb.wait()

    def what_is_network_number(self, args=""):
        """
        winn [ <addr> ]

        Send a What-Is-Network-Number message.  If the address is unspecified
        the message is locally broadcast.
        """
        args = args.split()

        # build a request
        try:
            request = WhatIsNetworkNumber()
            if len(args) > 0:
                request.pduDestination = Address(args[0])
            else:
                request.pduDestination = LocalBroadcast()
        except:
            print("invalid arguments")
            return

        iocb = IOCB((self.this_application.nsap.local_adapter, request))  # make an IOCB
        iocb.set_timeout(2)
        deferred(self.this_application.nse.request_io, iocb)
        iocb.wait()


rejectMessageToNetworkReasons = [
    "Other Error",
    "The router is not direclty connected to DNET and cannot find a router to DNET on any direclty connected network using Who-Is-Router-To-Network messages",
    "The tour is busy and unable to accept messages for the specified DNET at the present time",
    "It is an unknown network layer message",
    "The message is too long to be routed to this DNET",
    "The source message was rejected due to a BACnet security error and that error cannot be forwarded to the source device",
    "The source message was rejected due to errors in the addressing. The length of th DADR or SADR was determined to be invalid",
]
