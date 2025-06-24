import serial
import time
import threading
import binascii
from binascii import unhexlify
from crcmod import mkCrcFun

class LockerController:
    """
    智能保鲜快递柜后端控制器。
    该类封装了所有与硬件通信的逻辑，包括命令发送、数据接收和解析。
    """
    FRAME_HEADER = "FFFF"
    FRAME_END = "FFF7"

    def __init__(self, port, baudrate=38400, device_address=1, on_update_callback=None):
        """
        初始化控制器。

        :param port: 串口号, e.g., 'COM2'.
        :param baudrate: 波特率.
        :param device_address: 目标设备地址 (1-120).
        :on_update_callback: 回调函数，当状态更新时会被调用。
        """
        self.port = port
        self.baudrate = baudrate
        self.device_address_hex = self._int_to_hex_str(device_address, 1)
        self.on_update_callback = on_update_callback

        self.ser = None
        self.is_running = False
        self.listener_thread = None
        self.frame_num = 0

        # CRC-16/XMODEM 计算函数
        self.crc16_func = mkCrcFun(0x11021, rev=False, initCrc=0x0000, xorOut=0x0000)

        # --- 内部状态变量 ---
        # 这些变量由监听线程更新，并可通过 get_current_state() 获取
        self.lock = threading.Lock()  # 确保状态更新的线程安全
        self.state = {
            "connected": False,
            "last_update_time": 0,
            "device_code": "N/A",
            "device_address": device_address,
            "post_interval": 0,
            "compressor_delay": 0,
            "set_point_temp": 0.0,
            "temp_deviation": 0.0,
            "current_temp": 0.0,
            "compressor_status": "OFF",  # 'OFF', 'PRE_START', 'ON', 'FAULT'
            "system_status": "STOPPED", # 'STOPPED', 'PRE_START', 'RUNNING'
            "lock_status": [False] * 12  # 12个锁的状态 (0-11)
        }
        self.auto_compressor_enabled = False # 自动温控开关

    # --- 1. 连接与生命周期管理 ---

    def connect(self):
        """打开串口并启动后台监听线程。"""
        if self.is_running:
            print("控制器已在运行。")
            return True
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=0.2
            )
            self.is_running = True
            self.state["connected"] = True
            self.listener_thread = threading.Thread(target=self._listen_for_data, daemon=True)
            self.listener_thread.start()
            print(f"成功连接到串口 {self.port} 并启动监听。")
            return True
        except serial.SerialException as e:
            print(f"无法打开串口 {self.port}: {e}")
            self.state["connected"] = False
            return False

    def disconnect(self):
        """停止监听线程并关闭串口。"""
        if self.is_running:
            self.is_running = False
            if self.listener_thread:
                self.listener_thread.join(timeout=1)
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.state["connected"] = False
            print("控制器已断开连接。")

    def get_current_state(self):
        """获取当前快递柜的完整状态（线程安全）。"""
        with self.lock:
            return self.state.copy()

    # --- 2. 公共控制API (供Flask等上层应用调用) ---

    def set_temperature(self, temp_celsius : float):
        """
        设置目标温度。
        :param temp_celsius: 目标温度值 (整数, 0-63).
        """
        print(f"发送设置温度命令: {temp_celsius}°C")
        if not (0 <= temp_celsius <= 63):
            print("错误：温度必须在0-63度之间。")
            return
        
        # 编码温度值: 7bit符号(0)+6bit整数+1bit小数(0)
        temp_bin_str = ('0' if temp_celsius >= 0 else '1') + f'{int(abs(temp_celsius)):06b}' + ('0' if temp_celsius % 1 == 0 else '1')
        temp_hex_str = self._int_to_hex_str(int(temp_bin_str, 2), 1)

        frame = self._build_frame("0B", "04", temp_hex_str)
        self._send_frame(frame)

    def open_locks(self, lock_indices: list):
        """
        打开指定的抽屉。
        :param lock_indices: 一个包含要打开的锁的索引的列表, e.g., [1, 6] for 1, 6号抽屉
        """
        print(f"发送开锁命令, 目标抽屉索引: {lock_indices}")
        # 根据协议，控制位为2B (16bit)，0-11位有效
        control_mask = 0
        for index in lock_indices:
            index = index - 1  
            if 0 <= index <= 9:
                control_mask |= (1 << index)
        
        # 生成大端序的16进制字符串，例如 0021
        big_endian_hex = self._int_to_hex_str(control_mask, 2)
        
        # 将其字节反转以匹配小端序的设备，例如 0021 -> 2100
        little_endian_hex = big_endian_hex[2:4] + big_endian_hex[0:2]
        
        # 使用反转后的字节序来构建帧
        frame = self._build_frame("0c", "03", little_endian_hex)
        self._send_frame(frame)
        
        # box_hex = self._int_to_hex_str(control_mask, 2)
        # frame = self._build_frame("0c", "03", box_hex)
        # self._send_frame(frame)

    def control_compressor_manual(self, start: bool):
        """
        手动控制压缩机启停。这将禁用自动温控。
        :param start: True为启动, False为停止。
        """
        self.auto_compressor_enabled = False
        action = "01" if start else "00"
        action_text = "启动" if start else "停止"
        print(f"发送手动 {action_text} 压缩机命令。自动温控已关闭。")
        frame = self._build_frame("0b", "02", action)
        self._send_frame(frame)

    def enable_auto_compressor_control(self, enable: bool):
        """
        启用或禁用基于温度的自动压缩机控制。
        """
        self.auto_compressor_enabled = enable
        status = "启用" if enable else "禁用"
        print(f"自动温控已 {status}.")

    def set_device_params(self, code, addr, interval, delay, temp, deviation):
        """
        设置设备参数。
        """
        print("发送设置设备参数命令...")
        code_hex = code
        addr_hex = self._int_to_hex_str(addr, 1)
        interval_hex = self._int_to_hex_str(interval, 1)
        delay_hex = self._int_to_hex_str(delay, 1)
        
        temp_bin_str = '0' + f'{int(temp):06b}' + '0'
        temp_hex = self._int_to_hex_str(int(temp_bin_str, 2), 1)
        
        deviation_hex = self._int_to_hex_str(deviation, 1)

        data_payload = (
            f"{code_hex}{addr_hex}00{interval_hex}{delay_hex}0000"
            f"{temp_hex}{deviation_hex}ffffffff00"
        )
        frame = self._build_frame("1c", "05", data_payload)
        self._send_frame(frame)
        
        
    def set_system_parameters(self, params: dict):
        """
        设置终端的系统参数 (功能号 0x05)。
        这是一个广播命令，请确保总线上只有一个设备。

        :param params: 一个包含所有必要参数的字典。
                       例如: {
                           "device_code": "FFFFFFFF",
                           "device_address": 1,
                           "upload_interval": 1,
                           "compressor_delay": 30,
                           "set_temp": 4,
                           "temp_deviation": 2
                       }
        """
        print(f"发送设置系统参数命令，参数: {params}")

        try:
            # --- 1. 编码数据部分 (18字节) ---
            # 根据《终端控制板参数表》的格式进行编码
            
            # 设备编码 (5B) - 必须是10个十六进制字符
            device_code_hex = params['device_code'].ljust(10, 'F')[0:10]

            # 设备地址 (1B)
            address_hex = self._int_to_hex_str(params['device_address'], 1)

            # 备用 (1B)
            reserved1_hex = "00"

            # 状态上传时间间隔 T2 (1B)
            interval_hex = self._int_to_hex_str(params['upload_interval'], 1)

            # 压缩机启动延时 T3 (1B)
            delay_hex = self._int_to_hex_str(params['compressor_delay'], 1)

            # 备用 (2B)
            reserved2_hex = "0000"

            # 设定温度 (1B)
            temp_celsius = params['set_temp']
            temp_hex = self._encode_temperature_byte(temp_celsius)

            # 温度控制偏差 (1B)
            deviation_hex = self._int_to_hex_str(params['temp_deviation'], 1)

            # 备用 (2B + 2B + 1B = 5B) - 用FFFF...FF填充
            reserved3_hex = "FFFF"
            reserved4_hex = "FFFF"
            reserved5_hex = "00"

            # 拼接成完整的18字节数据载荷
            data_payload = (
                f"{device_code_hex}{address_hex}{reserved1_hex}{interval_hex}"
                f"{delay_hex}{reserved2_hex}{temp_hex}{deviation_hex}"
                f"{reserved3_hex}{reserved4_hex}{reserved5_hex}"
            )
            
            # --- 2. 构建并发送帧 ---
            # 注意：帧长是28 (0x1C), 功能号是 0x05
            # 设备地址使用广播地址 0x7F
            frame = self._build_frame_broadcast("1C", "05", data_payload)
            self._send_frame(frame)

        except KeyError as e:
            print(f"错误: 缺少必要的参数 '{e.args[0]}'")
        except Exception as e:
            print(f"构建设置参数帧时发生错误: {e}")

    def set_temperature_deviation(self, deviation: int):
        """
        设置温度控制偏差 (功能号 0x06)。
        这个值定义了自动温控时，当前温度允许偏离设定温度的范围。

        :param deviation: 偏差值，单位°C。必须是0-255之间的整数。
        """
        print(f"发送设置温度控制偏差命令，偏差值: {deviation}°C")

        # 1. 验证输入参数
        if not (0 <= deviation <= 255):
            print(f"错误: 偏差值 {deviation} 无效，必须在 0 到 255 之间。")
            return

        # 2. 准备数据部分 (1字节)
        # 直接将整数转换为1字节的16进制字符串
        data_hex = self._int_to_hex_str(deviation, 1)

        # 3. 构建并发送帧
        # 帧长是11 (0x0B), 功能号是 0x06
        frame = self._build_frame("0B", "06", data_hex)
        self._send_frame(frame)

    # --- 3. 内部工作方法 ---
    
    def _encode_temperature_byte(self, temp_celsius: float) -> str:
        """辅助函数：将温度值编码为1字节的16进制字符串。"""
        if not (-64 < temp_celsius < 64):
             print(f"警告: 温度 {temp_celsius} 超出可编码范围。")
        
        sign_bit = '1' if temp_celsius < 0 else '0'
        abs_temp = abs(temp_celsius)
        integer_part_bin = f'{int(abs_temp):06b}'
        epsilon = 1e-9
        fraction_bit = '1' if (abs_temp % 1) >= (0.5 - epsilon) else '0'
        
        temp_bin_str = sign_bit + integer_part_bin + fraction_bit
        return self._int_to_hex_str(int(temp_bin_str, 2), 1)

    def _build_frame_broadcast(self, length_hex, function_hex, data_hex):
        """辅助函数：构建一个使用广播地址0x7F的帧。"""
        self.frame_num = (self.frame_num % 255) + 1
        frame_num_hex = self._int_to_hex_str(self.frame_num, 1)

        # 使用广播地址 0x7F
        broadcast_address_hex = "7F"
        
        core_part = f"{length_hex}{frame_num_hex}{broadcast_address_hex}{function_hex}{data_hex}"
        crc = self._calculate_crc(core_part)
        return f"{self.FRAME_HEADER}{core_part}{crc}{self.FRAME_END}"

    def _listen_for_data(self):
        """在后台线程中运行，持续接收和处理来自串口的数据。"""
        while self.is_running:
            try:
                if self.ser.in_waiting > 0:
                    time.sleep(0.1)  # 等待数据接收完整
                    n = self.ser.in_waiting
                    payload = self.ser.read(n)
                    data_hex = binascii.b2a_hex(payload).decode()
                    
                    # CRC校验
                    if self._verify_crc(data_hex):
                        self._parse_frame(data_hex)
                    else:
                        print(f"接收到无效CRC帧: {data_hex}")

            except Exception as e:
                print(f"监听线程出错: {e}")
                self.is_running = False # 发生严重错误时退出
        print("监听线程已停止。")

    def _parse_frame(self, data_hex):
        state_updated = False # 标志位
        
        """解析合法的帧并更新内部状态。"""
        if len(data_hex) == 88: # 上传状态帧 (44字节)
            print(f"接收到状态帧: {data_hex}")
            with self.lock:
                # --- 锁状态解析---
                # 1. 提取小端序的16进制字符串, e.g., '03DE'
                little_endian_hex = data_hex[72:76]

                # 2. 将其字节反转以得到正确的大端数值, e.g., '03DE' -> 'DE03'
                if len(little_endian_hex) == 4:
                    big_endian_hex = little_endian_hex[2:4] + little_endian_hex[0:2]

                    # 3. 将正确的大端16进制字符串转换为整数
                    lock_int = int(big_endian_hex, 16)
                else:
                    # 如果数据长度不符，则认为状态为0，避免出错
                    lock_int = 0

                self.state["lock_status"] = [(lock_int >> i) & 1 == 1 for i in range(12)]

                # 设备编码
                self.state["device_code"] = data_hex[48:58]

                # 系统状态
                system_status_hex = data_hex[58:60]
                sys_status_map = {"00": "STOPPED", "01": "PRE_START", "02": "RUNNING"}
                self.state["system_status"] = sys_status_map.get(system_status_hex, "UNKNOWN")

                # 压缩机状态
                comp_status_hex = data_hex[62:64]
                status_map = {"00": "OFF", "01": "PRE_START", "02": "ON", "03": "FAULT"}
                self.state["compressor_status"] = status_map.get(comp_status_hex, "UNKNOWN")

                # 采集温度
                temp_hex = data_hex[66:68]
                self.state["current_temp"] = self._decode_temperature(temp_hex)

                # 设定温度
                set_temp_hex = data_hex[64:66]
                self.state["set_point_temp"] = self._decode_temperature(set_temp_hex)
                
                # 温控偏差
                self.state["temp_deviation"] = int(data_hex[36:38], 16)
                
                self.state["last_update_time"] = time.time()
                
                
                
                state_updated = True
                
            # 自动温控逻辑
            if self.auto_compressor_enabled:
                self._auto_manage_compressor()

        elif len(data_hex) == 28: # ACK帧 (14字节)
            print(f"接收到ACK帧: {data_hex}")
            # 可以根据需要解析ACK帧内容
            pass
        
            # 如果状态已更新并且设置了回调函数，则调用它
        if state_updated and self.on_update_callback:
            try:
                # 传递最新的状态副本
                self.on_update_callback(self.get_current_state())
            except Exception as e:
                print(f"执行更新回调时出错: {e}")

    def _auto_manage_compressor(self):
        """根据当前温度和设定值自动控制压缩机。"""
        with self.lock:
            current = self.state["current_temp"]
            set_point = self.state["set_point_temp"]
            deviation = self.state["temp_deviation"]
            
        upper_bound = set_point + deviation
        lower_bound = set_point - deviation
        
        # 如果当前温度高于上限，则启动压缩机
        if current > upper_bound:
            print("[自动温控] 温度过高，启动压缩机。")
            self._send_frame(self._build_frame("0b", "02", "01"))
        # 如果当前温度低于下限，则停止压缩机
        elif current < lower_bound:
            print("[自动温控] 温度已达标，停止压缩机。")
            self._send_frame(self._build_frame("0b", "02", "00"))

    def _decode_temperature(self, temp_hex):
        """从16进制字符串解码温度值。"""
        temp_int = int(temp_hex, 16)
        temp_bin = f'{temp_int:08b}'
        sign = -1 if temp_bin[0] == '1' else 1
        integer_part = int(temp_bin[1:7], 2)
        fraction_part = 0.5 if temp_bin[7] == '1' else 0.0
        return sign * (integer_part + fraction_part)

    def _build_frame(self, length_hex, function_hex, data_hex):
        """构建一个完整的待发送帧字符串。"""
        self.frame_num = (self.frame_num % 255) + 1
        frame_num_hex = self._int_to_hex_str(self.frame_num, 1)

        core_part = f"{length_hex}{frame_num_hex}{self.device_address_hex}{function_hex}{data_hex}"
        crc = self._calculate_crc(core_part)
        return f"{self.FRAME_HEADER}{core_part}{crc}{self.FRAME_END}"

    def _send_frame(self, frame_hex):
        """将16进制字符串帧转换为bytes并发送。"""
        if self.ser and self.ser.is_open:
            try:
                data_bytes = bytes.fromhex(frame_hex)
                self.ser.write(data_bytes)
                print(f"-> 已发送: {frame_hex}")
            except Exception as e:
                print(f"发送数据失败: {e}")
        else:
            print("错误: 串口未连接或已关闭。")

    def _calculate_crc(self, data_hex):
        """计算16进制字符串的CRC值。"""
        crc_out = hex(self.crc16_func(unhexlify(data_hex)))
        crc_data = crc_out[2:].zfill(4)
        return crc_data[2:] + crc_data[:2] # 返回小端格式的CRC

    def _verify_crc(self, received_hex):
        """验证接收到的16进制字符串帧的CRC。"""
        if len(received_hex) < 10: return False
        core_part = received_hex[4:-8]
        received_crc = received_hex[-8:-4]
        calculated_crc = self._calculate_crc(core_part)
        return received_crc == calculated_crc

    @staticmethod
    def _int_to_hex_str(value, byte_count):
        """将整数转换为指定字节数的16进制字符串，并左补零。"""
        return f'{value:0{byte_count*2}x}'

# --- 使用示例 (如何将此类用于后端) ---
if __name__ == "__main__":
    # 1. 查找可用串口 (方便调试)
    # ports = serial.tools.list_ports.comports()
    # print(ports)
    # if ports:
    #     print("可用串口:")
    #     for port in ports:
    #         print(f"- {port.device}")
    # else:
    #     print("未找到可用串口。请确保虚拟串口或物理设备已连接。")
    #     exit()

    # 2. 初始化控制器 (请修改为你的串口号)
    SERIAL_PORT = "COM2"  # <<<--- 修改为你的串口号
    controller = LockerController(port=SERIAL_PORT, device_address=1)

    # 3. 启动控制器
    if not controller.connect():
        print("无法启动控制器，程序退出。")
        exit()

    try:
        # 4. 模拟后端操作
        print("\n--- 启动自动温控 ---")
        controller.enable_auto_compressor_control(True)
        controller.set_temperature(25) # 设定目标温度为25度
        time.sleep(2)

        print("\n--- 模拟开锁操作 ---")
        controller.open_locks([0, 4, 9]) # 打开1号，5号，10号柜
        time.sleep(2)
        
        print("\n--- 模拟手动关闭压缩机 ---")
        controller.control_compressor_manual(start=False)
        time.sleep(2)

        # 5. 模拟Web服务器轮询获取状态以更新前端
        print("\n--- 开始轮询状态 (持续15秒) ---")
        for i in range(15):
            current_state = controller.get_current_state()
            # 在真实的Flask应用中，这里会将 current_state 转换为JSON并返回给前端
            print(f"[{i+1}/15] 实时温度: {current_state['current_temp']}°C, "
                  f"压缩机: {current_state['compressor_status']}, "
                  f"1号锁: {'ON' if current_state['lock_status'][0] else 'OFF'}")
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n程序被用户中断。")
    finally:
        # 6. 安全地断开连接
        print("\n--- 断开控制器连接 ---")
        controller.disconnect()
        print("程序结束。")