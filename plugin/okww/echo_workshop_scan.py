# 定义声骸工坊宿主任务入口并转接唯一包实现。
from echo_workshop.adapters.okww.scanning import EchoWorkshopScanTask as _Adapter


class EchoWorkshopScanTask(_Adapter):
    pass
