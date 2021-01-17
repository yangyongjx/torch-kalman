from math import pi
from typing import Optional, Tuple, Iterable, Union

from numpy import timedelta64, datetime64
import torch
from torch import jit, nn, Tensor
from torch_kalman.internals.utils import zpad

from torch_kalman.process.base import Process
from torch_kalman.process.regression import _RegressionBase
from torch_kalman.utils.features import fourier_tensor


class FourierSeason(_RegressionBase):
    def __init__(self,
                 id: str,
                 dt_unit: str,
                 period: float,
                 K: int,
                 measure: Optional[str] = None,
                 process_variance: bool = False,
                 decay: Optional[Tuple[float, float]] = None):
        """

        :param id:
        :param dt_unit: A string indicating the time-units used in the kalman-filter -- i.e., how far we advance with
        every timestep. Passed to `numpy.timedelta64(1, dt_unit)`.
        :param period: The number of `dt_units` it takes to get through a full season. Does not have to be an integer
        (e.g. 365.25 for yearly season on daily-data).
        :param K: The number of the fourier components
        :param measure:
        :param process_variance:
        :param decay:
        """
        dt_unit = timedelta64(1, dt_unit)
        self.dt_unit_ns = dt_unit / timedelta64(1, 'ns')
        assert self.dt_unit_ns.is_integer()
        self.dt_unit_ns = int(self.dt_unit_ns)

        state_elements = []
        for j in range(K):
            state_elements.append(f'sin{j}')
            state_elements.append(f'cos{j}')

        super().__init__(
            id=id,
            predictors=state_elements,
            measure=measure,
            h_module=TimesToFourier(K=K, seasonal_period=float(period)),
            process_variance=process_variance,
            decay=decay
        )
        self.h_kwarg = 'current_times'
        assert len(self.time_varying_kwargs) == 1
        self.time_varying_kwargs[0] = 'current_times'

    @jit.ignore
    def get_kwargs(self, kwargs: dict) -> Iterable[Tuple[str, str, str, Tensor]]:
        dt_unit = timedelta64(self.dt_unit_ns, 'ns')
        offsets = (kwargs['start_datetimes'].astype("datetime64[ns]") - datetime64(0, 'ns')) / dt_unit
        kwargs['current_times'] = torch.as_tensor(offsets.astype('float32')).view(-1, 1, 1) + kwargs['current_timestep']
        for found_key, key_name, key_type, value in Process.get_kwargs(self, kwargs):
            if found_key == 'current_times':
                found_key = 'start_datetimes'
            yield found_key, key_name, key_type, value


class TimesToFourier(nn.Module):
    def __init__(self, K: int, seasonal_period: float):
        super(TimesToFourier, self).__init__()
        self.K = K
        self.seasonal_period = float(seasonal_period)

    def forward(self, times: torch.Tensor):
        # import pdb;pdb.set_trace()
        return fourier_tensor(times, seasonal_period=self.seasonal_period, K=self.K).view(times.shape[0], self.K * 2)


class DiscreteSeason(Process):
    def __init__(self,
                 id: str,
                 num_seasons: int,
                 season_duration: int = 1,
                 measure: Optional[str] = None,
                 process_variance: bool = False,
                 decay: Optional[Tuple[float, float]] = None):
        f_modules = self._make_f_modules(num_seasons, season_duration, decay)
        state_elements = [zpad(i, n=len(str(num_seasons))) for i in range(num_seasons)]
        super(DiscreteSeason, self).__init__(
            id=id,
            state_elements=state_elements,
            measure=measure,
            h_tensor=torch.tensor([1.] + [0.] * (num_seasons - 1)),
            f_modules='TODO',
            f_kwarg='current_timestep',
            init_mean_kwargs=['start_datetimes'],
            time_varying_kwargs=['current_timestep'],
            no_pcov_state_elements=[] if process_variance else state_elements
        )

    def _make_f_modules(self,
                        num_seasons: int,
                        season_duration: int,
                        decay: Optional[Tuple[float, float]]) -> nn.ModuleDict:
        raise NotImplementedError


class TBATS(Process):
    def __init__(self,
                 id: str,
                 K: int,
                 period: str,
                 measure: Optional[str] = None):
        # needs to support:
        # - offsetting start based on start_datetimes
        # - decay
        raise NotImplementedError
        state_elements = []
        transitions = {}
        h_tensor = []
        for j in range(K):
            sj = f"s{j}"
            state_elements.append(sj)
            h_tensor.append(1.)
            s_star_j = f"s*{j}"
            state_elements.append(s_star_j)
            h_tensor.append(0.)
            lam = torch.tensor(2. * pi * j / period)
            transitions[f'{sj}->{sj}'] = torch.cos(lam)
            transitions[f'{sj}->{s_star_j}'] = -torch.sin(lam)
            transitions[f'{s_star_j}->{sj}'] = torch.sin(lam)
            transitions[f'{s_star_j}->{s_star_j}'] = torch.cos(lam)
        super(TBATS, self).__init__(
            id=id,
            state_elements=state_elements,
            f_tensors=transitions,
            h_tensor=torch.tensor(h_tensor),
            measure=measure
        )
