'''
Stanley Bak
run_f16_sim python version
'''

import time

import numpy as np
from scipy.integrate import RK45

from aerobench.highlevel.controlled_f16 import controlled_f16
from aerobench.util import get_state_names, Euler, StateIndex, print_state, Freezable

class F16SimState(Freezable):
    '''object containing simulation state

    With this interface you can run partial simulations, rather than having to simulate for the entire time bound

    if you just want a single run with a fixed time, it may be easier to use the run_f16_sim function
    '''

    def __init__(self, initial_state, ap, step=1/30, extended_states=False,
                integrator_str='rk45', v2_integrators=False, print_errors=True):

        self.model_str = model_str = ap.llc.model_str
        self.v2_integrators = v2_integrators
        initial_state = np.array(initial_state, dtype=float)

        self.step = step
        self.ap = ap
        self.print_errors = print_errors

        llc = ap.llc

        num_vars = len(get_state_names()) + llc.get_num_integrators()

        if initial_state.size < num_vars:
            # append integral error states to state vector
            x0 = np.zeros(num_vars)
            x0[:initial_state.shape[0]] = initial_state
        else:
            x0 = initial_state

        assert x0.size % num_vars == 0, f"expected initial state ({x0.size} vars) to be multiple of {num_vars} vars"

        self.times = times = [0]
        self.states = states = [x0]

        # mode can change at time 0
        ap.advance_discrete_mode(times[-1], states[-1])

        self.modes = [ap.mode]
        self.extended_states = extended_states

        if extended_states:
            xd, u, Nz, ps, Ny_r = get_extended_states(ap, times[-1], states[-1], model_str, v2_integrators)

            self.xd_list = [xd]
            self.u_list = [u]
            self.Nz_list = [Nz]
            self.ps_list = [ps]
            self.Ny_r_list = [Ny_r]

        self.der_func = make_der_func(ap, model_str, v2_integrators)

        if integrator_str == 'rk45':
            integrator_class = RK45
            self.integrator_kwargs = {}
        else:
            assert integrator_str == 'euler'
            integrator_class = Euler
            self.integrator_kwargs = {'step': step}

        self.integrator_class = integrator_class
        
        # note: fixed_step argument is unused by rk45, used with euler
        self.integrator = integrator_class(self.der_func, times[-1], states[-1], np.inf, **self.integrator_kwargs)
        self.cur_sim_time = 0

        self.total_sim_time = 0

        self.freeze_attrs()

    def simulate_to(self, tmax, tol=1e-7):
        '''simulate up to the passed in time

        this adds states to self.times, self.states, self.modes, and the other extended state lists if applicable 
        '''

        start = time.perf_counter()

        assert tmax >= self.cur_sim_time
        self.cur_sim_time = tmax

        ap = self.ap
        integrator = self.integrator
        times = self.times
        states = self.states
        modes = self.modes
        step = self.step

        assert integrator.status == 'running', f"integrator status was {integrator.status} in call to simulate_to()"

        while True:
            next_step_time = times[-1] + step

            if abs(times[-1] - tmax) > tol and next_step_time > tmax:
                # use a small last step
                next_step_time = tmax
            
            if next_step_time >= tmax + tol:
                # don't do any more steps
                break

            # goal for rest of the loop: do one more step

            while next_step_time >= integrator.t + tol:
                # keep advancing integrator until it goes past the next step time
                integrator.step()
                if integrator.status != 'running':
                    break

            if integrator.status != 'running':
                break

            # get the state at next_step_time
            times.append(next_step_time)

            if abs(integrator.t - next_step_time) < tol:
                states.append(integrator.x)
            else:
                dense_output = integrator.dense_output()
                states.append(dense_output(next_step_time))

            mode_changed = ap.advance_discrete_mode(times[-1], states[-1])
            modes.append(ap.mode)

            # re-run dynamics function at current state to get non-state variables
            if self.extended_states:
                xd, u, Nz, ps, Ny_r = get_extended_states(ap, times[-1], states[-1],
                                                          self.model_str, self.v2_integrators)

                self.xd_list.append(xd)
                self.u_list.append(u)

                self.Nz_list.append(Nz)
                self.ps_list.append(ps)
                self.Ny_r_list.append(Ny_r)

            if ap.is_finished(times[-1], states[-1]):
                # this both causes the outer loop to exit and sets res['status'] appropriately
                integrator.status = 'autopilot finished'
                break

            if mode_changed:
                # re-initialize the integration class on discrete mode switches
                self.integrator = integrator = self.integrator_class(self.der_func, times[-1], states[-1], np.inf,
                                                                     **self.integrator_kwargs)

        if integrator.status == 'failed' and self.print_errors:
            print(f'Warning: integrator status was "{integrator.status}"')

        self.total_sim_time += time.perf_counter() - start

def run_f16_sim(initial_state, tmax, ap, step=1/30, extended_states=False,
                integrator_str='rk45', v2_integrators=False, print_errors=True):
    '''Simulates and analyzes autonomous F-16 maneuvers

    if multiple aircraft are to be simulated at the same time,
    initial_state should be the concatenated full (including integrators) initial state.

    returns a dict with the following keys:

    'status': integration status, should be 'finished' if no errors, or 'autopilot finished'
    'times': time history
    'states': state history at each time step
    'modes': mode history at each time step

    if extended_states was True, result also includes:
    'xd_list' - derivative at each time step
    'ps_list' - ps at each time step
    'Nz_list' - Nz at each time step
    'Ny_r_list' - Ny_r at each time step
    'u_list' - input at each time step, input is 7-tuple: throt, ele, ail, rud, Nz_ref, ps_ref, Ny_r_ref
    These are tuples if multiple aircraft are used
    '''

    fss = F16SimState(initial_state, ap, step, extended_states,
                integrator_str, v2_integrators, print_errors)

    fss.simulate_to(tmax)

    assert abs(fss.times[-1] - tmax) < 1e-7, f"asked for simulation to time {tmax} with step {step}, " + \
      f"got final time {fss.times[-1]}"

    # extract states

    res = {}
    res['status'] = fss.integrator.status
    res['times'] = fss.times
    res['states'] = np.array(fss.states, dtype=float)
    res['modes'] = fss.modes

    if extended_states:
        res['xd_list'] = fss.xd_list
        res['ps_list'] = fss.ps_list
        res['Nz_list'] = fss.Nz_list
        res['Ny_r_list'] = fss.Ny_r_list
        res['u_list'] = fss.u_list

    res['runtime'] = fss.total_sim_time

    return res

class SimModelError(RuntimeError):
    'simulation state went outside of what the model is capable of simulating'

def make_der_func(ap, model_str, v2_integrators):
    'make the combined derivative function for integration'

    def der_func(t, full_state):
        'derivative function, generalized for multiple aircraft'

        u_refs = ap.get_checked_u_ref(t, full_state)

        num_aircraft = u_refs.size // 4
        num_vars = len(get_state_names()) + ap.llc.get_num_integrators()
        assert full_state.size // num_vars == num_aircraft

        xds = []

        for i in range(num_aircraft):
            state = full_state[num_vars*i:num_vars*(i+1)]

            #print(f".called der_func(aircraft={i}, t={t}, state={full_state}")

            alpha = state[StateIndex.ALPHA]
            if not -2 < alpha < 2:
                raise SimModelError(f"alpha ({alpha}) out of bounds")

            vel = state[StateIndex.VEL]
            # even going lower than 300 is probably not a good idea
            if not 200 <= vel <= 3000:
                raise SimModelError(f"velocity ({vel}) out of bounds")

            alt = state[StateIndex.ALT]
            if not -10000 < alt < 100000:
                raise SimModelError(f"altitude ({alt}) out of bounds")

            u_ref = u_refs[4*i:4*(i+1)]

            xd = controlled_f16(t, state, u_ref, ap.llc, model_str, v2_integrators)[0]
            xds.append(xd)

        rv = np.hstack(xds)

        return rv

    return der_func

def get_extended_states(ap, t, full_state, model_str, v2_integrators):
    '''get xd, u, Nz, ps, Ny_r at the current time / state

    returns tuples if more than one aircraft
    '''

    llc = ap.llc
    num_vars = len(get_state_names()) + llc.get_num_integrators()
    num_aircraft = full_state.size // num_vars

    xd_tup = []
    u_tup = []
    Nz_tup = []
    ps_tup = []
    Ny_r_tup = []

    u_refs = ap.get_checked_u_ref(t, full_state)

    for i in range(num_aircraft):
        state = full_state[num_vars*i:num_vars*(i+1)]
        u_ref = u_refs[4*i:4*(i+1)]

        xd, u, Nz, ps, Ny_r = controlled_f16(t, state, u_ref, llc, model_str, v2_integrators)

        xd_tup.append(xd)
        u_tup.append(u)
        Nz_tup.append(Nz)
        ps_tup.append(ps)
        Ny_r_tup.append(Ny_r)

    if num_aircraft == 1:
        rv_xd = xd_tup[0]
        rv_u = u_tup[0]
        rv_Nz = Nz_tup[0]
        rv_ps = ps_tup[0]
        rv_Ny_r = Ny_r_tup[0]
    else:
        rv_xd = tuple(xd_tup)
        rv_u = tuple(u_tup)
        rv_Nz = tuple(Nz_tup)
        rv_ps = tuple(ps_tup)
        rv_Ny_r = tuple(Ny_r_tup)

    return rv_xd, rv_u, rv_Nz, rv_ps, rv_Ny_r
