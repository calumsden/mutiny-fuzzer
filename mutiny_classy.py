#!/usr/bin/env python
#------------------------------------------------------------------
# November 2014, created within ASIG
# Author James Spadaro (jaspadar)
# Co-Author Lilith Wyatt (liwyatt)
#------------------------------------------------------------------
# Copyright (c) 2014-2017 by Cisco Systems, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the Cisco Systems, Inc. nor the
#    names of its contributors may be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS "AS IS" AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#------------------------------------------------------------------
#
# This is the main fuzzing script.  It takes a .fuzzer file and performs the
# actual fuzzing
#
#------------------------------------------------------------------

import datetime
import errno
import imp
import os.path
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import argparse
import ssl
from copy import deepcopy
from backend.proc_director import ProcDirector
from backend.fuzzer_types import Message, MessageCollection, Logger
from backend.packets import PROTO,IP
from mutiny_classes.mutiny_exceptions import *
from mutiny_classes.message_processor import MessageProcessorExtraParams
from backend.fuzzerdata import FuzzerData
from backend.menu_functions import validateNumberRange

# Path to Radamsa binary
RADAMSA=os.path.abspath( os.path.join(__file__, "../radamsa-v0.6/bin/radamsa") )
# Whether to print debug info
DEBUG_MODE=False

# TODO, clean up monitor code
# if there are multiple fuzzers, but want the same monitor for all of them
global_monitor = None
wantGlobalMonitor = True

class MutinyFuzzer():

    def __init__(self, args):
        self.args=args
        # Test number to start from, 0 default
        self.MIN_RUN_NUMBER=0
        # Test number to go to, -1 is unlimited
        self.MAX_RUN_NUMBER=-1
        # For seed loop, finite range to repeat
        self.SEED_LOOP = []
        # For dumpraw option, dump into log directory by default, else 'dumpraw'
        self.DUMPDIR = ""

        #Populate global arguments from parseargs
        self.fuzzerFilePath = args.prepped_fuzz
        self.host = args.target_host

        #Assign Lower/Upper bounds on test cases as needed
        if args.range:
            (self.MIN_RUN_NUMBER, self.MAX_RUN_NUMBER) = getRunNumbersFromArgs(args, args.range)
        elif args.loop:
            self.SEED_LOOP = validateNumberRange(args.loop,True)

        #Check for dependency binaries
        if not os.path.exists(RADAMSA):
            sys.exit("Could not find radamsa in %s... did you build it?" % RADAMSA)

        #Logging options
        self.isReproduce = False
        self.logAll = False

        if args.quiet:
            self.isReproduce = True
        elif args.logAll:
            self.logAll = True

        self.outputDataFolderPath = os.path.join("%s_%s" % (os.path.splitext(self.fuzzerFilePath)[0], "logs"), datetime.datetime.now().strftime("%Y-%m-%d,%H%M%S"))
        self.fuzzerFolder = os.path.abspath(os.path.dirname(self.fuzzerFilePath))
        
        ########## Declare variables for scoping, "None"s will be assigned below
        self.messageProcessor = None
        
        ###Here we read in the fuzzer file into a dictionary for easier variable propagation
        self.optionDict = {"unfuzzedBytes":{}, "message":[]}
        
        self.fuzzerData = FuzzerData()
        print "Reading in fuzzer data from %s..." % (self.fuzzerFilePath)
        self.fuzzerData.readFromFile(self.fuzzerFilePath)


        #clumsden TODO - make this pretty, maybe add field to fuzzerData
        #clumsden - hacky way to start http fuzzer at a different seed
        if self.fuzzerData.port == 80 and self.fuzzerData.proto == "tcp":
            self.MIN_RUN_NUMBER = 58247530
        if self.fuzzerData.proto == "udp": #clumsden, set udp fuzzer at different seed
            self.MIN_RUN_NUMBER = 13243441
        #clumsden end


        ######## Processor Setup ################
        # The processor just acts as a container #
        # class that will import custom versions #
        # messageProcessor/exceptionProessor/    #
        # monitor, if they are found in the      #
        # process_dir specified in the .fuzzer   #
        # file generated by fuzz_prep.py         #
        ##########################################

        # Assign options to variables, error on anything that's missing/invalid
        self.processorDirectory = self.fuzzerData.processorDirectory
        if self.processorDirectory == "default":
            # Default to fuzzer file folder
            self.processorDirectory = self.fuzzerFolder
        else:
            # Make sure fuzzer file path is prepended
            self.processorDirectory = os.path.join(self.fuzzerFolder, self.processorDirectory)
        
        #Create class director, which import/overrides processors as appropriate
        self.procDirector = ProcDirector(self.processorDirectory)

        global global_monitor
        self.monitor = None
        ########## Launch child monitor thread
            ### monitor.task = spawned thread
            ### monitor.crashEvent = threading.Event()
        if wantGlobalMonitor==True and global_monitor == None:
            global_monitor = self.procDirector.startMonitor(self.host,self.fuzzerData.port)
            print global_monitor
            time.sleep(10) #clumsden added so pid_watcher has time to connect to monitor

        #! make it so logging message does not appear if reproducing (i.e. -r x-y cmdline arg is set)
        self.logger = None
        
        if not self.isReproduce:
            print "Logging to %s" % (self.outputDataFolderPath)
            self.logger = Logger(self.outputDataFolderPath)
        
        if self.args.dumpraw:
            if not isReproduce:
                self.DUMPDIR = self.outputDataFolderPath
            else:
                self.DUMPDIR = "dumpraw"
                try:
                    os.mkdir("dumpraw")
                except:
                    print "Unable to create dumpraw dir"
                    pass
        
        
        self.exceptionProcessor = self.procDirector.exceptionProcessor()
        self.messageProcessor = self.procDirector.messageProcessor()

        ########## Begin fuzzing
        self.i = self.MIN_RUN_NUMBER-1 if self.fuzzerData.shouldPerformTestRun else self.MIN_RUN_NUMBER
        self.failureCount = 0
        self.loop_len = len(self.SEED_LOOP) # if --loop


    #will run one seed of the current instance of MutinyFuzzer
    def fuzz(self):
        args = self.args
        fuzzerData = self.fuzzerData
        host = self.host
        messageProcessor = self.messageProcessor

        retryRun = True
        while retryRun:
            retryRun = False 
            lastMessageCollection = deepcopy(fuzzerData.messageCollection)
            wasCrashDetected = False
            print "\n** Sleeping for %.3f seconds **" % args.sleeptime
            time.sleep(args.sleeptime)
    
            try:
                try:
                    print "\n\n%s: " % (self.fuzzerFilePath)
                    if args.dumpraw:
                        print "Performing single raw dump case: %d" % args.dumpraw
                        self.performRun(fuzzerData, host, self.logger, messageProcessor, seed=args.dumpraw)
                    elif self.i == self.MIN_RUN_NUMBER-1:
                        print "Performing test run without fuzzing..."
                        self.performRun(fuzzerData, host, self.logger, messageProcessor, seed=-1)
                    elif self.loop_len:
                        print "Fuzzing with seed %d" % (self.SEED_LOOP[self.i%loop_len])
                        self.performRun(fuzzerData, host, self.logger, messageProcessor, seed=self.SEED_LOOP[self.i%loop_len])
                    else:
                        print "Fuzzing with seed %d" % (self.i)
                        self.performRun(fuzzerData, host, self.logger, messageProcessor, seed=self.i)
                    #if --quiet, (self.logger==None) => AttributeError
                    if self.logAll:
                        try:
                            self.logger.outputLog(self.i, fuzzerData.messageCollection, "LogAll ")
                        except AttributeError:
                            pass
    
                    #clumsden - so a crash detected by monitor is registered
                    if global_monitor.crashEvent.isSet():
                        raise Exception()
                    #clumsden end
    
    
                except Exception as e:
                    if global_monitor.crashEvent.isSet():
                        print "Crash event detected"
                        try:
                            self.logger.outputLog(self.i, fuzzerData.messageCollection, "Crash event detected")
                            exit() #clumsden - have this commented out if you don't want to stop after a crash is detected
                        except AttributeError:
                            pass
                        global_monitor.crashEvent.clear()
        
                    elif self.logAll:
                        try:
                            self.logger.outputLog(self.i, fuzzerData.messageCollection, "LogAll ")
                        except AttributeError:
                            pass
        
                    if e.__class__ in MessageProcessorExceptions.all:
                        # If it's a MessageProcessorException, assume the MP raised it during the run
                        # Otherwise, let the MP know about the exception
                        raise e
                    else:
                        self.exceptionProcessor.processException(e)
                        # Will not get here if processException raises another exception
                        print "Exception ignored: %s" % (str(e))
    
            except LogCrashException as e:
                if self.failureCount == 0:
                    try:
                        print "MessageProcessor detected a crash"
                        self.logger.outputLog(self.i, fuzzerData.messageCollection, str(e))
                    except AttributeError:
                        pass
        
                if self.logAll:
                    try:
                        logger.outputLog(i, fuzzerData.messageCollection, "LogAll ")
                    except AttributeError:
                        pass

                self.failureCount = self.failureCount + 1
                wasCrashDetected = True
        
            except AbortCurrentRunException as e:
                # Give up on the run early, but continue to the next test
                # This means the run didn't produce anything meaningful according to the processor
                print "Run aborted: %s" % (str(e))
        
            except RetryCurrentRunException as e:
                # Same as AbortCurrentRun but retry the current test rather than skipping to next
                print "Retrying current run: %s" % (str(e))
                # Slightly sketchy - a continue *should* just go to the top of the while without changing i
                retryRun = True
                continue
        
            except LogAndHaltException as e:
                if self.logger:
                    self.logger.outputLog(self.i, fuzzerData.messageCollection, str(e))
                    print "Received LogAndHaltException, logging and halting"
                else:
                    print "Received LogAndHaltException, halting but not logging (quiet mode)"
                exit()

            except LogLastAndHaltException as e:
                if self.logger:
                    if self.i > self.MIN_RUN_NUMBER:
                        print "Received LogLastAndHaltException, logging last run and halting"
                        if self.MIN_RUN_NUMBER == self.MAX_RUN_NUMBER:
                            #in case only 1 case is run
                            self.logger.outputLastLog(self.i, lastMessageCollection, str(e))
                            print "Logged case %d" % self.i
                        else:
                            self.logger.outputLastLog(self.i-1, lastMessageCollection, str(e))
                    else:
                        print "Received LogLastAndHaltException, skipping logging (due to last run being a test run) and halting"
                else:
                    print "Received LogLastAndHaltException, halting but not logging (quiet mode)"
                exit()
        
            except HaltException as e:
                print "Received HaltException halting"
                exit()
        
            if wasCrashDetected:
                if self.failureCount < fuzzerData.failureThreshold:
                    print "Failure %d of %d allowed for seed %d" % (self.failureCount, fuzzerData.failureThreshold, self.i)
                    print "The test run didn't complete, continuing after %d seconds..." % (fuzzerData.failureTimeout)
                    time.sleep(fuzzerData.failureTimeout)
                else:
                    print "Failed %d times, moving to next test." % (self.failureCount)
                    self.failureCount = 0
                    self.i += 1
            else:
                self.i += 1
        
            # Stop if we have a maximum and have hit it
            if self.MAX_RUN_NUMBER >= 0 and self.i > self.MAX_RUN_NUMBER:
                exit()
        
            if args.dumpraw:
                exit()



    # Perform a fuzz run.
    # If seed is -1, don't perform fuzzing (test run)
    def performRun(self,fuzzerData, host, logger, messageProcessor, seed=-1):
        # Before doing anything, set up logger
        # Otherwise, if connection is refused, we'll log last, but it will be wrong
        if logger != None:
            logger.resetForNewRun()
    
        # We don't perform DNS resolution, but always automatically type "localhost"
        # ... really need to go ahead and add DNS resolution soon
        if host == "localhost":
            host = "127.0.0.1"
    
        # cheap testing for ipv6/ipv4/unix
        # don't think it's worth using regex for this, since the user
        # will have to actively go out of their way to subvert this.
        if "." in host:
            socket_family = socket.AF_INET
            addr = (host,fuzzerData.port)
        elif ":" in host:
            socket_family = socket.AF_INET6
            addr = (host,fuzzerData.port)
        else:
            socket_family = socket.AF_UNIX
            addr = (host)
    
        #just in case filename is like "./asdf" !=> AF_INET
        if "/" in host:
            socket_family = socket.AF_UNIX
            addr = (host)
    
        # Call messageprocessor preconnect callback if it exists
        try:
            messageProcessor.preConnect(seed, host, fuzzerData.port)
        except AttributeError:
            pass

        # for TCP/UDP/RAW support
        if fuzzerData.proto == "tcp":
            connection = socket.socket(socket_family,socket.SOCK_STREAM)
            # Don't connect yet, until after we do any binding below
        elif fuzzerData.proto == "tls":
            try:
                _create_unverified_https_context = ssl._create_unverified_context
            except AttributeError:
                # Legacy Python that doesn't verify HTTPS certificates by default
                pass
            else:
                # Handle target environment that doesn't support HTTPS verification
                ssl._create_default_https_context = _create_unverified_https_context
            tcpConnection = socket.socket(socket_family,socket.SOCK_STREAM)
            connection = ssl.wrap_socket(tcpConnection)
            # Don't connect yet, until after we do any binding below
        elif fuzzerData.proto == "udp":
            connection = socket.socket(socket_family,socket.SOCK_DGRAM)
        # PROTO = dictionary of assorted L3 proto => proto number
        # e.g. "icmp" => 1
        elif fuzzerData.proto in PROTO:
            connection = socket.socket(socket_family,socket.SOCK_RAW,PROTO[fuzzerData.proto])
            if fuzzerData.proto != "raw":
                connection.setsockopt(socket.IPPROTO_IP,socket.IP_HDRINCL,0)
            addr = (host,0)
            try:
                connection = socket.socket(socket_family,socket.SOCK_RAW,PROTO[fuzzerData.proto])
            except Exception as e:
                print e
                print "Unable to create raw socket, please verify that you have sudo access"
                sys.exit(0)
        elif fuzzerData.proto == "L2raw":
            connection = socket.socket(socket.AF_PACKET,socket.SOCK_RAW,0x0300)
            connection.bind(('ens160', 0)) #clumsden TODO replace hardcoded iface for layer2 traffic
            #clumsden TODO - experimental branch has addr=(host,0)
        else:
            addr = (host,0)
            try:
                #test if it's a valid number
                connection = socket.socket(socket_family,socket.SOCK_RAW,int(fuzzerData.proto))
                connection.setsockopt(socket.IPPROTO_IP,socket.IP_HDRINCL,0)
            except Exception as e:
                print e
                print "Unable to create raw socket, please verify that you have sudo access"
                sys.exit(0)

        if fuzzerData.proto == "tcp" or fuzzerData.proto == "udp" or fuzzerData.proto == "tls":
            # Specifying source port or address is only supported for tcp and udp currently
            if fuzzerData.sourcePort != -1:
                # Only support right now for tcp or udp, but bind source port address to something
                # specific if requested
                if fuzzerData.sourceIP != "" or fuzzerData.sourceIP != "0.0.0.0":
                    connection.bind((fuzzerData.sourceIP, fuzzerData.sourcePort))
                else:
                    # User only specified a port, not an IP
                    connection.bind(('0.0.0.0', fuzzerData.sourcePort))
            elif fuzzerData.sourceIP != "" and fuzzerData.sourceIP != "0.0.0.0":
                # No port was specified, so 0 should auto-select
                connection.bind((fuzzerData.sourceIP, 0))
        if fuzzerData.proto == "tcp" or fuzzerData.proto == "tls":
            # Now that we've had a chance to bind as necessary, connect
            connection.connect(addr)

        i=0
        for i in range(0, len(fuzzerData.messageCollection.messages)):
            message = fuzzerData.messageCollection.messages[i]
    
            # Go ahead and revert any fuzzing or messageprocessor changes before proceeding
            message.resetAlteredMessage()
    
            if message.isOutbound():
                # Primarily used for deciding how to handle preFuzz/preSend callbacks
                doesMessageHaveSubcomponents = len(message.subcomponents) > 1
    
                # Get original subcomponents for outbound callback only once
                originalSubcomponents = map(lambda subcomponent: subcomponent.getOriginalByteArray(), message.subcomponents)
    
                if doesMessageHaveSubcomponents:
                    # For message with subcomponents, call prefuzz on fuzzed subcomponents
                    for j in range(0, len(message.subcomponents)):
                        subcomponent = message.subcomponents[j]
                        # Note: we WANT to fetch subcomponents every time on purpose
                        # This way, if user alters subcomponent[0], it's reflected when
                        # we call the function for subcomponent[1], etc
                        actualSubcomponents = map(lambda subcomponent: subcomponent.getAlteredByteArray(), message.subcomponents)
                        prefuzz = messageProcessor.preFuzzSubcomponentProcess(subcomponent.getAlteredByteArray(), MessageProcessorExtraParams(i, j, subcomponent.isFuzzed, originalSubcomponents, actualSubcomponents))
                        subcomponent.setAlteredByteArray(prefuzz)
                else:
                    # If no subcomponents, call prefuzz on ENTIRE message
                    actualSubcomponents = map(lambda subcomponent: subcomponent.getAlteredByteArray(), message.subcomponents)
                    prefuzz = messageProcessor.preFuzzProcess(actualSubcomponents[0], MessageProcessorExtraParams(i, -1, message.isFuzzed, originalSubcomponents, actualSubcomponents))
                    message.subcomponents[0].setAlteredByteArray(prefuzz)
    
                # Skip fuzzing for seed == -1
                if seed > -1:
                    # Now run the fuzzer for each fuzzed subcomponent
                    for subcomponent in message.subcomponents:
                        if subcomponent.isFuzzed:
                            radamsa = subprocess.Popen([RADAMSA, "--seed", str(seed)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            byteArray = subcomponent.getAlteredByteArray()
                            (fuzzedByteArray, error_output) = radamsa.communicate(input=byteArray)
                            fuzzedByteArray = bytearray(fuzzedByteArray)
                            subcomponent.setAlteredByteArray(fuzzedByteArray)

                # Fuzzing has now been done if this message is fuzzed
                # Always call preSend() regardless for subcomponents if there are any
                if doesMessageHaveSubcomponents:
                    for j in range(0, len(message.subcomponents)):
                        subcomponent = message.subcomponents[j]
                        # See preFuzz above - we ALWAYS regather this to catch any updates between
                        # callbacks from the user
                        actualSubcomponents = map(lambda subcomponent: subcomponent.getAlteredByteArray(), message.subcomponents)
                        presend = messageProcessor.preSendSubcomponentProcess(subcomponent.getAlteredByteArray(), MessageProcessorExtraParams(i, j, subcomponent.isFuzzed, originalSubcomponents, actualSubcomponents))
                        subcomponent.setAlteredByteArray(presend)
    
                # Always let the user make any final modifications pre-send, fuzzed or not
                actualSubcomponents = map(lambda subcomponent: subcomponent.getAlteredByteArray(), message.subcomponents)
                byteArrayToSend = messageProcessor.preSendProcess(message.getAlteredMessage(), MessageProcessorExtraParams(i, -1, message.isFuzzed, originalSubcomponents, actualSubcomponents))
    
                if self.args.dumpraw:
                    loc = os.path.join(DUMPDIR,"%d-outbound-seed-%d"%(i,self.args.dumpraw))
                    if message.isFuzzed:
                        loc+="-fuzzed"
                    with open(loc,"wb") as f:
                        f.write(repr(str(byteArrayToSend))[1:-1])
    
                self.sendPacket(connection, addr, byteArrayToSend)
            else:
                # Receiving packet from server
                messageByteArray = message.getAlteredMessage()
                data = self.receivePacket(connection,addr,len(messageByteArray))
                if data == messageByteArray:
                    print "\tReceived expected response"
                if logger != None:
                    logger.setReceivedMessageData(i, data)
    
                messageProcessor.postReceiveProcess(data, MessageProcessorExtraParams(i, -1, False, [messageByteArray], [data]))
    
                if self.args.dumpraw:
                    loc = os.path.join(DUMPDIR,"%d-inbound-seed-%d"%(i,self.args.dumpraw))
                    with open(loc,"wb") as f:
                        f.write(repr(str(data))[1:-1])
    
            if logger != None:
                logger.setHighestMessageNumber(i)
    
    
            i += 1
    
        connection.close()


    def receivePacket(self, connection, addr, bytesToRead):
        readBufSize = 4096
        connection.settimeout(self.fuzzerData.receiveTimeout)
    
        if connection.type == socket.SOCK_STREAM or connection.type == socket.SOCK_DGRAM:
            response = bytearray(connection.recv(readBufSize))
        else:
            response = bytearray(connection.recvfrom(readBufSize,addr))
    
    
        if len(response) == 0:
            # If 0 bytes are recv'd, the server has closed the connection
            # per python documentation
            raise ConnectionClosedException("Server has closed the connection")
        if bytesToRead > readBufSize:
            # If we're trying to read > 4096, don't actually bother trying to guarantee we'll read 4096
            # Just keep reading in 4096 chunks until we should have read enough, and then return
            # whether or not it's as much data as expected
            i = readBufSize
            while i < bytesToRead:
                response += bytearray(connection.recv(readBufSize))
                i += readBufSize
    
        print "\tReceived %d bytes" % (len(response))
        if DEBUG_MODE:
            print "\tReceived: %s" % (response)
        return response


    # Takes a socket and outbound data packet (byteArray), sends it out.
    # If debug mode is enabled, we print out the raw bytes
    def sendPacket(self, connection, addr, outPacketData):
        connection.settimeout(self.fuzzerData.receiveTimeout)
        if connection.type == socket.SOCK_STREAM:
            connection.send(outPacketData)
        elif connection.type == socket.SOCK_RAW: #for L2raw clumsden start
            connection.send(outPacketData) # clumsden, ran into trouble with this later and needed sendto(outPacketData,addr)...what it was orginally?
        else:
            connection.sendto(outPacketData,addr)
    
        print "\tSent %d byte packet" % (len(outPacketData))
        if DEBUG_MODE:
            print "\tSent: %s" % (outPacketData)
            print "\tRaw Bytes: %s" % (Message.serializeByteArray(outPacketData))




#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Set up signal handler for CTRL+C and signals from child monitor thread
# since this is the same signal, we use the global_monitor.crashEvent flag()
# to differentiate between a CTRL+C and a interrupt_main() call from child
def sigint_handler(signal, frame):
    if not global_monitor.crashEvent.isSet():
        # No event = quit
        # Quit on ctrl-c
        print "\nSIGINT received, stopping\n"
        sys.exit(0)

signal.signal(signal.SIGINT, sigint_handler)

#----------------------------------------------------
# Set MIN_RUN_NUMBER and MAX_RUN_NUMBER when provided
# by the user below
def getRunNumbersFromArgs(args, strArgs):
    if "-" in strArgs:
        testNumbers = strArgs.split("-")
        if len(testNumbers) == 2:
            if len(testNumbers[1]): #e.g. strArgs="1-50"
                return (int(testNumbers[0]), int(testNumbers[1]))
            else:                   #e.g. strArgs="3-" (equiv. of --skip-to)
                return (int(testNumbers[0]),-1)
        else: #e.g. strArgs="1-2-3-5.."
             sys.exit("Invalid test range given: %s" % args)
    else:
        # If they pass a non-int, allow this to bomb out
        return (int(strArgs),int(strArgs))
#----------------------------------------------------


#this is not in MutinyFuzzer class, called in main
#returns instance of MutinyFuzzer
def get_mutiny_with_args(prog_args):
    #TODO: add description/license/ascii art print out??
    desc =  "======== The Mutiny Fuzzing Framework =========="
    epi = "==" * 24 + '\n'
    
    parser = argparse.ArgumentParser(description=desc,epilog=epi)
    parser.add_argument("prepped_fuzz", help="Path to file.fuzzer")
    parser.add_argument("target_host", help="Target to fuzz")
    parser.add_argument("-s","--sleeptime",help="Time to sleep between fuzz cases (float)",type=float,default=0)
    seed_constraint = parser.add_mutually_exclusive_group()
    seed_constraint.add_argument("-r", "--range", help="Run only the specified cases. Acceptable arg formats: [ X | X- | X-Y ], for integers X,Y")
    seed_constraint.add_argument("-l", "--loop", help="Loop/repeat the given finite number range. Acceptible arg format: [ X | X-Y | X,Y,Z-Q,R | ...]")
    seed_constraint.add_argument("-d", "--dumpraw", help="Test single seed, dump to 'dumpraw' folder",type=int)
    
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", help="Don't log the outputs",action="store_true")
    verbosity.add_argument("--logAll", help="Log all the outputs",action="store_true")
    
    args = parser.parse_args()

    fuzzer_files = []
    if os.path.isdir(args.prepped_fuzz):
        fuzzer_files = [os.path.join(args.prepped_fuzz, f) for f in os.listdir(args.prepped_fuzz) if os.path.isfile(os.path.join(args.prepped_fuzz, f))]
    else:
        fuzzer_files.append(args.prepped_fuzz)

    fuzzers = []
    for f in fuzzer_files:
        args.prepped_fuzz = f
        try:
            fuzzers.append(MutinyFuzzer(args))
        except Exception as e:
            raise e
    return fuzzers

if __name__ == "__main__":
    # Usage case
    if len(sys.argv) < 3:
        sys.argv.append('-h')

    #clumsden - wrapped original code to loop through the .fuzzer files
    fuzzers = get_mutiny_with_args(sys.argv[1:])

    while True:
        for fuzzer in fuzzers:
            try:
                fuzzer.fuzz()
            except KeyboardInterrupt:
                fuzzer.sigint_handler(1) 



