#!/usr/bin/env python
"""
Jarvis Schultz
Feb 2014

This node runs a true receding horizon optimization.


SUBSCRIPTIONS:
    - meas_config (PlanarSystemConfig)

PUBLISHERS:
    - filt_config (PlanarSystemConfig)
    - ref_config (PlanarSystemConfig)
    - serial_commands (RobotCommands)
    - mass_ref_path (Path)
    - mass_filt_path (Path)
    - post_covariance (PlanarCovariance)
    - filt_state (PlanarSystemState)
    - start_time (Time)

SERVICES:
    - get_ref_config (PlanarSystemService) (provider)

PARAMS:
    - window_length ~ defines number of timesteps in an optimization
    - controller_freq ~ defines the rate that everything is happening at
    - time_final ~ defines the total time to run the controller
    - robot_index ~ which robot to communicate with
"""

# ROS imports:
import roslib; roslib.load_manifest('receding_planar_sys')
import rospy
import tf
from std_msgs.msg import Time
from puppeteer_msgs.msg import PlanarSystemConfig
from puppeteer_msgs.msg import RobotCommands
from puppeteer_msgs.msg import PlanarCovariance
from puppeteer_msgs.msg import PlanarSystemState
from puppeteer_msgs.srv import PlanarSystemService
from puppeteer_msgs.srv import PlanarSystemServiceRequest
from nav_msgs.msg import Path
from geometry_msgs.msg import PoseStamped

# misc imports:
import numpy as np

# local imports
import optimizer as op
import reference_manager as rm
import system_definition as sd
import ekf


# global constants:
DT = 1/10. # timestep
LEN =  20 # number of time steps to optimize over
Tper = rm.Tper


class RecedingController:

    def __init__(self):
        rospy.loginfo("Creating RecedingController class...")

        # first let's get all necessary parameters:
        self.get_and_set_params()

        # create time vectors, optimizer, dsys
        self.system = sd.MassSystem2D()
        self.mvi = sd.trep.MidpointVI(self.system)
        self.tvec = np.arange(0, self.dt*(int(self.tf/self.dt)+1), self.dt)
        self.twin = np.arange(0, self.dt*self.n_win, self.dt)
        self.dsys = op.discopt.DSystem(self.mvi, self.twin)

        # get the initial config of the system:
        X,U = rm.calc_reference_traj(self.dsys, [0])
        self.X0 = X[0]
        self.Q0 = self.X0[0:self.system.nQ]

        # create EKF:
        self.meas_cov = np.diag((0.5,0.5,0.5,0.5)) # measurement covariance
        self.proc_cov = np.diag((0.1,0.1,0.1,0.1,0.15,0.15,0.15,0.15)) # process covariance
        self.Hk = np.hstack((np.eye(self.system.nQ),
                             np.zeros((self.system.nQ, self.system.nQ))))
        self.ekf = ekf.VI_EKF('vi_ekf', self.X0, self.dt, self.proc_cov, self.meas_cov,
                              Hk=self.Hk, sys=self.system, L=len(self.tvec))
 
        # define some vars that we are going to need
        self.first_flag = True
        self.tbase = rospy.Time.now()
        self.callback_count = 0
        self.Uprev = np.zeros(self.dsys.nU)

        # create a service provider for the reference configuration
        self.config_serv = rospy.Service("get_ref_config", PlanarSystemService,
                                            self.ref_config_service_handler)
        # define subscribers:
        self.meas_sub = rospy.Subscriber("meas_config", PlanarSystemConfig,
                                            self.meascb)
        # define publishers:
        self.time_pub = rospy.Publisher("start_time", Time)
        self.filt_pub = rospy.Publisher("filt_config", PlanarSystemConfig)
        self.filt_state_pub = rospy.Publisher("filt_state", PlanarSystemState)
        self.ref_pub = rospy.Publisher("ref_config", PlanarSystemConfig)
        self.comm_pub = rospy.Publisher("serial_commands", RobotCommands)
        # self.ref_path_pub = rospy.Publisher("mass_ref_path", Path)
        # self.filt_path_pub = rospy.Publisher("mass_filt_path", Path)
        self.cov_pub = rospy.Publisher("post_covariance", PlanarCovariance)

        # send the robot it's starting pose in the /optimization_frame
        rospy.logwarn("Waiting for three seconds!!!")
        time.sleep(3)
        rospy.loginfo("Ready to go!!!")
        self.send_initial_config()
        # send robot a start command:
        self.send_start_command()

        return
        


    def ref_config_service_handler(self, req):
        if req.index != 0:
            rospy.logerr("Current provider cannot handle index request")
            return None
        elif req.t < 0.0 or req.t > self.tf:
            rospy.logerr("Requested time %f is outside of valid horizon", req.t)
            return None
        # get X,U at requested time
        X,U = rm.calc_reference_traj(self.dsys, [req.t])
        # fill out message
        config = PlanarSystemConfig()
        config.xm = X[0,self.system.get_config('xm').index]
        config.ym = X[0,self.system.get_config('ym').index]
        config.xr = X[0,self.system.get_config('xr').index]
        config.r = X[0,self.system.get_config('r').index]
        config.header.frame_id = "/optimization_frame"
        config.header.stamp = rospy.Time.now()
        time = req.t
        length = len(self.tvec)
        xtmp = X[0]
        return {'config' : config,
                'state' : xtmp,
                'dt' : dt,
                'length' : length,
                'time' : time,
                }

    

    
    def meascb(self, data):
        rospy.logdebug("measurement callback triggered")

        op = rospy.get_param("/operating_condition")
        # increment callback count
        self.callback_count += 1;

        if op != 2:
            # we are not running, keep setting initializations
            self.first_flag = True
            self.callback_count = 0
            # clear trajectories:
            return

        if self.first_flag:
            rospy.loginfo("Beginning Trajectory")
            self.send_initial_config()
            self.tbase = data.header.stamp
            self.first_flag = False
            # get reference traj after initial dt:
            Xtmp,Utmp = rm.calc_reference_traj(self.dsys, [0, dt])
            # send reference traj U and store:
            self.Uprev = Utmp[0]
            self.convert_and_send_input(self.Uprev)
        else:
            # first, let's update the EKF
            
            self.ekf.step_filter

        
        

        
        return

    
    def get_and_set_params(self):
        # controller frequency
        if rospy.has_param("controller_freq"):
            tmp = rospy.get_param("controller_freq")
            self.dt = 1/float(tmp)
        else:
            tmp = 1/DT
            self.dt = DT
            rospy.set_param("controller_freq", tmp)
        rospy.loginfo("Controller frequency = %f Hz (dt = %f sec.)", tmp, self.dt)
        # window length
        if rospy.has_param("window_length"):
            tmp = rospy.get_param("window_length")
            self.n_win = int(tmp)
        else:
            self.n_win = LEN
            rospy.set_param("window_length", self.n_win)
        rospy.loginfo("Window Length: %d",self.n_win)
        # controller run time
        if rospy.has_param("time_final"):
            tmp = rospy.get_param("time_final")
            self.tf = float(tmp)
        else:
            self.n_win = Tper
            rospy.set_param("time_final", self.n_win)
        rospy.loginfo("Final Time: %d",self.)
        # idle on startup
        rospy.set_param("/operating_condition", 0)
        return
        


    def send_initial_config(self):
        com = RobotCommands()
        com.robot_index = self.robot_index
        com.type = ord('a')
        com.header.stamp = rospy.get_rostime()
        com.header.frame_id = "/optimization_frame"
        com.div = 4
        com.x = self.Q0[self.system.get_config['xr'].index]
        com.y = 0
        com.th = 0
        com.height_left = self.Q0[self.system.get_config['r'].index]
        com.height_right = 1
        self.comm_pub.publish(com)
        return


    def send_start_command(self):
        com = RobotCommands()
        com.robot_index = self.robot_index
        com.type = ord('m')
        com.header.stamp = rospy.get_rostime()
        self.comm_pub.publish(com)
        return

    
    def convert_and_send_input(self, u2):
        """
        This function takes in an input (kinematic configs at t_{k+1}) and it
        calculates the velocities between our current best estimate of where we
        are and those positions.  Then sends those velocities to the robot.
        """
        u1 = self.ekf.xkk[self.system.nQd:self.system.nQ]
        ucom = (u2-u1)/self.dt
        com = RobotCommands()
        com.robot_index = self.robot_index
        com.type = ord('i')
        com.v_robot = ucom[0]
        com.w_robot = 0
        com.rdot = 0
        com.rdot_left = ucom[1]
        com.rdot_right = 0
        com.div = 3
        self.comm_pub.publish(com)
        return





def main():
    """
    Run the main loop, by instatiating a System class, and then
    calling ros.spin
    """
    rospy.init_node('cl_control', log_level=rospy.INFO)

    try:
        controller = RecedingController()
    except rospy.ROSInterruptException: pass

    rospy.spin()


if __name__=='__main__':
    main()
