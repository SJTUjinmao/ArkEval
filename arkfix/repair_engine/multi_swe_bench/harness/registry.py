import threading
import json
from multi_swe_bench.harness.constant import KEY_INSTANCE_ID
import os

data_registry = dict()
lock = threading.Lock()

def register_data(pr_file):
    global data_registry

    # 获取当前脚本的绝对路径
    current_script_path = os.path.abspath(__file__)
    # 获取当前脚本所在目录
    current_directory = os.path.dirname(current_script_path)
    # 定义文件的相对路径
    relative_file_path = os.path.join('../../', pr_file)
    # 获取文件的绝对路径
    registered_data_path = os.path.join(current_directory, relative_file_path)

    if data_registry is not None and len(data_registry.keys()) > 0:
        print(f"data {registered_data_path} is already registered.")
        return

    # 请求锁
    lock.acquire()
    try:
        # 注册资源
        try:
            with open(registered_data_path, "r", encoding="utf-8") as f:
                data_list = [json.loads(line) for line in f]
        except UnicodeDecodeError:
            # Windows may default to GBK when encoding is not explicit elsewhere.
            # Some datasets are saved as UTF-8 with BOM.
            with open(registered_data_path, "r", encoding="utf-8-sig") as f:
                data_list = [json.loads(line) for line in f]

        data_registry.update({data[KEY_INSTANCE_ID]: data for data in data_list})
    finally:
        # 释放锁
        lock.release()
    print(f"register data {registered_data_path} success.")

def get_data(instance_id: str):
    if data_registry is None or len(data_registry.keys()) == 0:
        register_data()
    data = data_registry.get(instance_id)
    if data is None:
        raise RuntimeError(f"data {instance_id} not found in data registry.")

    return data
