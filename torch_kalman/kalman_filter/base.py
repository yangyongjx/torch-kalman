from typing import TypeVar, Optional, Callable, List, Union

import numpy as np
import torch
from numpy.core.multiarray import ndarray
from torch import Tensor
from torch.nn import ParameterList
from tqdm import tqdm

from torch_kalman.design import Design
from torch_kalman.design.for_batch import DesignForBatch
from torch_kalman.state_belief import Gaussian, StateBelief
from torch_kalman.state_belief.over_time import StateBeliefOverTime
from torch_kalman.utils import identity


class KalmanFilter(torch.nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.design: Design = None
        self._init_design(*args, **kwargs)

        # parameters from design:
        self.design_parameters = ParameterList()
        for param in self.design.parameters():
            self.design_parameters.append(param)

        # the distributional family, implemented by property (default gaussian)
        self._family = None

        self.to(device=self.design.device)

    def _init_design(self, *args, **kwargs) -> None:
        self.design = Design(*args, **kwargs)

    @property
    def measure_size(self) -> int:
        return self.design.measure_size

    @property
    def family(self) -> TypeVar('Gaussian'):
        if self._family is None:
            self._family = Gaussian
        return self._family

    def predict_initial_state(self, design_for_batch: DesignForBatch) -> 'Gaussian':
        return self.family(means=design_for_batch.initial_mean, covs=design_for_batch.initial_covariance)

    # noinspection PyShadowingBuiltins
    def forward(self,
                input: Tensor,
                initial_state: Optional[StateBelief] = None,
                **kwargs) -> StateBeliefOverTime:
        """
        :param input: The multivariate time-series to be fit by the kalman-filter. A Tensor where the first dimension
        represents the groups, the second dimension represents the time-points, and the third dimension represents the
        measures.
        :param initial_state: If a StateBelief, this is used as the prediction for time=0; if None then each process
        generates initial values.
        :param kwargs: Other kwargs that will be passed to the `design_for_batch` method.
        :return: A StateBeliefOverTime consisting of one-step-ahead predictions.
        """

        kwargs = kwargs.copy()

        num_groups, num_timesteps, num_measures = input.shape
        if num_measures != self.measure_size:
            raise ValueError(f"This KalmanFilter has {self.measure_size} measurement-dimensions; but the input shape is "
                             f"{(num_groups, num_timesteps, num_measures)} (last dim should == measure-size).")

        design_for_batch = self.design.for_batch(num_groups=num_groups,
                                                 num_timesteps=num_timesteps,
                                                 **kwargs)

        # initial state of the system:
        if initial_state is None:
            state_prediction = self.predict_initial_state(design_for_batch)
        else:
            state_prediction = initial_state

        prog = kwargs.pop('progress', identity) or identity
        if prog is True:
            prog = tqdm
        iterator = prog(range(num_timesteps))

        # generate one-step-ahead predictions:
        state_predictions = []
        for t in iterator:
            if t > 0:
                # take state-prediction of previous t (now t-1), correct it according to what was actually measured at at t-1
                state_belief = state_prediction.update(obs=input[:, t - 1, :])

                # predict the state for t, from information from t-1
                # F at t-1 is transition *from* t-1 *to* t
                F = design_for_batch.F(t - 1)
                Q = design_for_batch.Q(t - 1)
                state_prediction = state_belief.predict(F=F, Q=Q)

            # compute how state-prediction at t translates into measurement-prediction at t
            H = design_for_batch.H(t)
            R = design_for_batch.R(t)
            state_prediction.compute_measurement(H=H, R=R)

            # append to output:
            state_predictions.append(state_prediction)

        return self.family.concatenate_over_time(state_beliefs=state_predictions,
                                                 design=self.design,
                                                 start_datetimes=kwargs.get('start_datetimes', None))

    def smooth(self, states: StateBeliefOverTime):
        raise NotImplementedError

    def simulate(self,
                 states: StateBeliefOverTime,
                 horizon: int,
                 num_iter: int,
                 from_datetimes: Optional[ndarray] = None,
                 state_belief_to_measurements: Optional[Callable] = None,
                 **kwargs) -> List[Tensor]:

        assert horizon > 0

        kwargs = kwargs.copy()

        # forecast-from time:
        if isinstance(states, StateBelief):
            initial_state = states
        elif from_datetimes is None:
            initial_state = states.last_prediction
        else:
            initial_state = states.slice_by_dt(datetimes=from_datetimes)
            if 'start_datetimes' not in kwargs.keys():
                kwargs['start_datetimes'] = np.tile(from_datetimes, num_iter)

        initial_state = initial_state.__class__(means=initial_state.means.repeat((num_iter, 1)),
                                                covs=initial_state.covs.repeat((num_iter, 1, 1)),
                                                last_measured=initial_state.last_measured.repeat(num_iter))

        design_for_batch = self.design.for_batch(num_groups=initial_state.num_groups,
                                                 num_timesteps=horizon,
                                                 **kwargs)

        trajectories = initial_state._simulate_state_trajectories(design_for_batch=design_for_batch,
                                                                  **kwargs)

        if state_belief_to_measurements is None:
            state_belief_to_measurements = lambda traj: traj.measurement_distribution.sample()

        sim = state_belief_to_measurements(trajectories)
        return torch.chunk(sim, num_iter)

    def forecast(self,
                 states: Union[StateBeliefOverTime, StateBelief],
                 horizon: int,
                 forecast_from_datetimes: Optional[ndarray] = None,
                 **kwargs) -> StateBeliefOverTime:

        assert horizon > 0

        kwargs = kwargs.copy()

        # forecast-from time:
        if forecast_from_datetimes is not None:
            kwargs['start_datetimes'] = forecast_from_datetimes
        if isinstance(states, StateBelief):
            state_prediction = states
        else:
            if forecast_from_datetimes is None:
                state_prediction = states.last_prediction
            else:
                state_prediction = states.slice_by_dt(datetimes=forecast_from_datetimes)

        design_for_batch = self.design.for_batch(num_groups=state_prediction.num_groups,
                                                 num_timesteps=horizon,
                                                 **kwargs)

        prog = kwargs.pop('progress', identity) or identity
        if prog is True:
            prog = tqdm
        iterator = prog(range(horizon))

        forecasts = []
        for t in iterator:
            if t > 0:
                # predict the state for t, from information from t-1
                # F at t-1 is transition *from* t-1 *to* t
                F = design_for_batch.F(t - 1)
                Q = design_for_batch.Q(t - 1)
                state_prediction = state_prediction.predict(F=F, Q=Q)

            # compute how state-prediction at t translates into measurement-prediction at t
            H = design_for_batch.H(t)
            R = design_for_batch.R(t)
            state_prediction.compute_measurement(H=H, R=R)

            # append to output:
            forecasts.append(state_prediction)

        return self.family.concatenate_over_time(state_beliefs=forecasts,
                                                 design=self.design,
                                                 start_datetimes=forecast_from_datetimes)