# 为工作台提供策略资料导入及显式代理更新作业。
from concurrent.futures import ThreadPoolExecutor

from ww_echo_workshop.application.policy_data import PolicyDataStore
from ww_echo_workshop.resources.policy_updates import PolicyDataUpdater
from ww_echo_workshop.resources.wuwa_updates import WuwaUpdateService


class PolicyResourceJobs:
    def __init__(self, data_dir):
        self._store = PolicyDataStore(data_dir)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='policy-data')

    def load_with_source(self):
        return self._store.load_with_source()

    def import_file(self, path):
        return self._store.version(self._store.import_file(path))

    def _update_policy(self, url):
        final, pack = PolicyDataUpdater(self._store).update(url)
        return final, self._store.version(pack)

    def submit_policy(self, url):
        return self._pool.submit(self._update_policy, url)

    def submit_wuwa(self, url):
        return self._pool.submit(WuwaUpdateService(self._store).update, url)

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
