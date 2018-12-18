#!/usr/bin/env python

# Copyright (c) 2018 Christoph Pilz
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

import abc
import carla
import math
import random
import sys
import threading
import time

from collections import deque

from .control import InputController
from .observer import IObserver
from .present import MondeoPlayerAgentHandler
from .util import Action, Pose, TimeStamp
from timed_event_handler import TimedEventHandler


class Actor(IObserver, threading.Thread):
    def __init__(self, actorType, name, events, enableLogging, pose, speed, timestamp):
        threading.Thread.__init__(self)

        self.name = name

        self._actorType = actorType
        self._name = name
        self._events = events
        self._isLogging = enableLogging
        self._isConnected = False
        self._isRunning = False
        self._actionQueue = None
        self._currentPose = None
        self._currentSpeed = None
        self._currentTimeStamp = None
        self._executionQueue = None
        self._desiredPose = pose
        self._desiredSpeed = speed
        self._desiredTimeStamp = timestamp
        self._dataExchangeLock = threading.Lock()
        self._wakeUp = threading.Event()
        self._timeOut = 10.0
        self._client = None

    def getName(self):
        return self._name

    def getIsConnected(self):
        self._dataExchangeLock.acquire()
        isConnected = self._isConnected
        self._dataExchangeLock.release()
        return isConnected

    def getIsRunning(self):
        self._dataExchangeLock.acquire()
        isRunning = self._isRunning
        self._dataExchangeLock.release()
        return isRunning

    def setInit(self, speed, pose):
        self._desiredSpeed = speed
        self._desiredPose = pose
        self._desiredTimeStamp = TimeStamp()

    def addAction(self, action):
        self._dataExchangeLock.acquire()
        self._actionQueue.append(action)
        self._dataExchangeLock.release()

    def setAction(self, action):
        self._dataExchangeLock.acquire()
        self._actionQueue = deque([action])
        self._executionQueue = None
        self._dataExchangeLock.release()

    def startActing(self):
        if self._isConnected == False:
            print("[Error][Actor::startActing]", self._name, "not connected, cannot start acting")
            return
        if self._isRunning == True:
            print("[Warning][Actor::startActing]", self._name, "already running")
            return

        self._isRunning = True
        threading.Thread.start(self)

    def stopActing(self):
        self._isRunning = False
        self.join()

    # threading.Thread mehtods
    def start(self):
        print("[INFO][Actor::start] Don't use this")
        self.startActing()

    def run(self):
        self._actorThread()

    def update(self, event):
        raise NotImplementedError("implement update")

    @abc.abstractmethod
    def connectToSimulatorAndEvenHandler(self, ipAddress, port, timeout):
        pass

    @abc.abstractmethod
    def disconnectFromSimulatorAndEventHandler(self):
        pass

    @abc.abstractmethod
    def _actorThread(self):
        pass


class CarlaActor(Actor):
    def __init__(self, actorType, name, events=deque(), enableLogging=False, pose=None, speed=None, timestamp=None):
        Actor.__init__(self, actorType, name, events, enableLogging, pose, speed, timestamp)
        self.__carlaActor = None
        self.__inputController = None

    def connectToSimulatorAndEvenHandler(self, ipAddress, port, timeout):
        try:
            self._client = carla.Client(ipAddress, port)
            self._client.set_timeout(timeout)
            print("# try spawning actor", self._name)
            blueprint = self._client.get_world().get_blueprint_library().find("vehicle.lincoln.mkz2017")
            # blueprint = self._client.get_world().get_blueprint_library().find("vehicle.ford.mustang")
            transform = carla.Transform(
                carla.Location(x=self._desiredPose.getPosition()[0],
                               y=self._desiredPose.getPosition()[1],
                               z=self._desiredPose.getPosition()[2]),
                carla.Rotation(roll=math.degrees(self._desiredPose.getOrientation()[0]),
                               pitch=math.degrees(self._desiredPose.getOrientation()[1]),
                               yaw=math.degrees(self._desiredPose.getOrientation()[2])))
            self.__carlaActor = self._client.get_world().spawn_actor(blueprint, transform)
            if self.__carlaActor is None:
                print("TODO-FIX DEBUG: Couldn't spawn actor")
                # raise RuntimeError("Couldn't spawn actor")

            TimedEventHandler().subscribe(self._name, self.update)
            self._isConnected = True
        except:
            print("[Error][CarlaActor::connectToSimulatorAndEvenHandler] Unexpected error:", sys.exc_info())
            self._isConnected = False
        finally:
            return self._isConnected

    def disconnectFromSimulatorAndEventHandler(self):
        try:
            print("# destroying actor", self._name)
            self._isConnected = False
            TimedEventHandler().unsubscribe(self._name)
            self.__carlaActor.destroy()
            self.__carlaActor = None
            return True
        except:
            print("[Error][CarlaActor::disconnectFromSimulatorAndEventHandler] Unexpected error:", sys.exc_info())
            self._isConnected = False
            return False

    def addEntityEvent(self, event):
        self._dataExchangeLock.acquire()
        self._events.append(event)
        self._dataExchangeLock.release()
    
    def checkConditionTriggered(self, sc):
        isConditionTriggered = False

        if sc.pose != None:
            distance = math.sqrt(pow(self._currentPose.getPosition()[0]-sc.pose.getPosition()[0], 2.0) + 
                                 pow(self._currentPose.getPosition()[1]-sc.pose.getPosition()[1], 2.0) + 
                                 pow(self._currentPose.getPosition()[2]-sc.pose.getPosition()[2], 2.0))
            if distance < sc.tolerance:
                isConditionTriggered = True
            else:
                isConditionTriggered = False
        else:
            print("[WARNING][CarlaActor::handleEvents] Implementation Missing. This should not be reached")

        return isConditionTriggered


    def handleEvents(self):
        # check the startconditions
        for event in self._events:
            sc = event.getStartCondition()
            if sc != None:
                if sc.isConditionTrue:
                    pass
                elif sc.isConditionMetEdge:
                    if TimedEventHandler.getCurrentSimTimeStamp().getFloat() >= sc.timestampConditionTriggered.getFloat() + sc.delay:
                        sc.isConditionTrue = True
                    else:
                        sc.isConditionTrue = False
                elif sc.isConditionTriggered:
                    if sc.edge == "falling" or sc.edge == "any":
                        sc.isConditionMetEdge = not self.checkConditionTriggered(sc)
                    else:
                        print("[WARNING][CarlaActor::handleEvents] Implementation Missing. This should not be reached")
                else:
                    sc.isConditionTriggered = self.isConditionTriggered(sc)
                    if sc.isConditionTriggered and (sc.edge == "rising" or sc.edge == "any"):
                        sc.isConditionMetEdge = True
                        if sc.delay == 0.0:
                            sc.isConditionTrue = True
                        else:
                            sc.timestampConditionTriggered = TimedEventHandler.getCurrentSimTimeStamp()
                    else:
                        pass # edge falling
        
        remainingEvents = deque()
        while(len(self._events) > 0):
            event = self._events.popleft()
            if event.getStartCondition().isConditionTrue:
                if event.getStartCondition == "overwrite":
                    if hasattr(event, "getActors"):
                        for actor in event.getActors():
                            actor.setAction(event.getAction())
                    else:
                        self.setAction(event.getAction)
                else:
                    print("[WARNING][CarlaActor::handleEvents] Implementation Missing. This should not be reached")
            else:
                remainingEvents.append(event)
        self._events = remainingEvents


    def handleExecutionQueue(self):
        # check if queue full
        if self._executionQueue is not None:
            if len(self._executionQueue) > 0:
                return len(self._executionQueue)

        # queue empty or not initialized
        self._executionQueue = deque([])

        # TODO implement magic execution queue stuff
        print("# TODO implement magic action queue stuff")
        pose = self._desiredPose
        timestamp = self._desiredTimeStamp
        self._executionQueue.append(Action(pose, timestamp))

        return len(self._executionQueue)

    def handleEgo(self):
        # send data to ROS
        MondeoPlayerAgentHandler().process(self.__carlaActor)
        MondeoPlayerAgentHandler().processGodSensor(self.__carlaActor)

        # receive data from ROS
        if self.__inputController is None:
            self.__inputController = InputController()
        cur_control = self.__inputController.get_cur_control()

        # send data to carla
        carla_vehicle_control = carla.VehicleControl(cur_control["throttle"],
                                                     cur_control["steer"],
                                                     cur_control["brake"],
                                                     cur_control["hand_brake"],
                                                     cur_control["reverse"])
        self.__carlaActor.apply_control(carla_vehicle_control)

    def handleNonEgo(self):
        # do magic pose stuff
        self._desiredPose = self._currentPose
        speedMS = self._desiredSpeed * 1000.0 / 3600
        diffS = TimedEventHandler().getSimTimeDiff()
        distanceM = speedMS * diffS

        # TODO event handling / route calculation goes here
        #print("TODO get event from deque and execute")

        # send data to Carla
        transform = carla.Transform(carla.Location(self._desiredPose.getPosition()[0] - distanceM,
                                                   self._desiredPose.getPosition()[1],
                                                   self._desiredPose.getPosition()[2]),
                                    carla.Rotation(math.degrees(self._desiredPose.getOrientation()[1]),
                                                   math.degrees(self._desiredPose.getOrientation()[2]),
                                                   math.degrees(self._desiredPose.getOrientation()[0])))

        self.__carlaActor.set_transform(transform)

    def update(self, event):
        if event is None:
            pass  # just a normal tick
        else:
            self._dataExchangeLock.acquire()
            self._events.append(event)
            self._dataExchangeLock.release()

        self._wakeUp.set()

    def _actorThread(self):
        print (self._name, "started acting")
        while(self._isRunning):
            self._dataExchangeLock.acquire()

            try:
                if self._isConnected == False:
                    raise RuntimeError("Actor is not connected to the simulator")

                # receive Carla data
                transform = self.__carlaActor.get_transform()
                self._currentPose = Pose(transform.location.x,
                                         transform.location.y,
                                         transform.location.z,
                                         math.radians(transform.rotation.roll),
                                         math.radians(transform.rotation.pitch),
                                         math.radians(transform.rotation.yaw))
                velocity = self.__carlaActor.get_velocity()
                self._currentSpeed = math.sqrt(pow(velocity.x, 2.0) + pow(velocity.y, 2.0) + pow(velocity.z, 2.0))
                self._currentTimeStamp = TimedEventHandler().getCurrentSimTimeStamp()
                # print(self._name, self._currentTimeStamp.getFloat(), self._currentPose, self._currentSpeed)

                # handle data, send to carla - trigger magic stuff
                if(self._name == "Ego"):
                    self.handleEgo()
                else:
                    self.handleNonEgo()

            except:
                print("[Error][CarlaActor::_actorThread] Unexpected error:", sys.exc_info())
                self._isRunning = False

            self._dataExchangeLock.release()

            if not self._wakeUp.wait(self._timeOut):
                print("[Error][CarlaActor::_actorThread] WakeUp Timeout")
                self._isRunning = False

            self._wakeUp.clear()

        print (self._name, "stopped acting")
