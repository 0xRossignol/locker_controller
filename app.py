import time
import atexit
from flask import Flask, jsonify, request, render_template
from flask_socketio import SocketIO, emit
from flask_cors import CORS

from locker_controller import LockerController

# --- 全局配置 ---
SERIAL_PORT = "COM2"
DEVICE_ADDRESS = 1
FLASK_HOST = '0.0.0.0' 
FLASK_PORT = 5000

# --- 应用初始化 ---
app = Flask(__name__)
# 设置一个密钥，用于保护session
app.config['SECRET_KEY'] = 'just-a-secret-key' 

# --- 跨域配置 (CORS) ---
# 为所有的HTTP路由启用CORS
#    origins="*" 表示允许任何域名的请求。
#    CORS(app, origins=["http://localhost:3000", "http://your-frontend-domain.com"])
CORS(app, resources={r"/api/*": {"origins": "*"}})
# 配置SocketIO以允许跨域连接
#    cors_allowed_origins="*" 同样表示允许任何来源的WebSocket连接。
#    同样，在生产环境中应指定具体来源。
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")

# --- 回调函数定义 ---
def broadcast_status_update(state_data):
    """
    这个函数会被LockerController调用。
    它的作用是通过WebSocket向所有连接的客户端广播最新的状态。
    """
    print(f"通过WebSocket广播状态: {state_data}")
    # 'update_status' 是自定义的事件名
    # namespace='/' 表示广播给默认命名空间下的所有客户端
    socketio.emit('update_status', state_data, namespace='/')

# --- 创建并启动控制器 (关键步骤) ---
# 创建一个全局的、唯一的控制器实例
# 这个实例将在整个Flask应用的生命周期内存在
print("正在初始化快递柜控制器...")
controller = LockerController(
        port=SERIAL_PORT, 
        device_address=DEVICE_ADDRESS,
        on_update_callback=broadcast_status_update
)

# 注册一个程序退出时执行的函数，用于安全地断开串口连接
@atexit.register
def shutdown_controller():
    print("Flask应用正在关闭，断开控制器连接...")
    controller.disconnect()

# 在Flask应用启动前，连接到控制器
if not controller.connect():
    print("!!!!!!!!!! 严重错误 !!!!!!!!!!")
    print(f"无法连接到串口 {SERIAL_PORT}。API可能无法正常工作。")
    print("请检查物理连接、虚拟串口配置和权限。")
else:
    print("控制器连接成功，Flask应用准备就绪。")


# --- API 路由定义 ---

@app.route('/')
def index():
    """提供一个简单的欢迎页面和API文档链接。"""
    return f"""
    <h1>智能快递柜后端API</h1>
    <p>服务正在运行中...</p>
    <p><b>API 端点:</b></p>
    <ul>
        <li><code>GET /api/status</code> - 获取快递柜所有实时状态</li>
        <li><code>POST /api/temperature</code> - 设置目标温度</li>
        <li><code>POST /api/locks/open</code> - 打开指定的锁</li>
        <li><code>POST /api/compressor/manual</code> - 手动控制压缩机</li>
        <li><code>POST /api/compressor/auto</code> - 启用/禁用自动温控</li>
    </ul>
    """

@app.route('/api/status', methods=['GET'])
def get_status():
    """获取快递柜的当前完整状态。"""
    current_state = controller.get_current_state()
    return jsonify(current_state)

@app.route('/api/temperature', methods=['POST'])
def set_temperature():
    """
    设置目标温度。
    需要一个JSON请求体，例如: {"temperature": 25}
    """
    data = request.get_json()
    if not data or 'temperature' not in data:
        return jsonify({"error": "请求体中缺少 'temperature' 字段"}), 400

    try:
        temp = float(data['temperature'])
        controller.set_temperature(temp)
        return jsonify({"status": "success", "message": f"设置温度命令已发送: {temp}°C"})
    except (ValueError, TypeError):
        return jsonify({"error": "无效的温度值，必须是整数"}), 400

@app.route('/api/locks/open', methods=['POST'])
def open_locks():
    """
    打开一个或多个锁。
    需要一个JSON请求体，例如: {"indices": [1, 6]}
    """
    data = request.get_json()
    if not data or 'indices' not in data or not isinstance(data['indices'], list):
        return jsonify({"error": "请求体中缺少 'indices' 字段或其不是一个列表"}), 400
    
    try:
        indices = [int(i) for i in data['indices']]
        controller.open_locks(indices)
        return jsonify({"status": "success", "message": f"开锁命令已发送，目标索引: {indices}"})
    except (ValueError, TypeError):
        return jsonify({"error": "无效的索引值，必须是整数列表"}), 400

@app.route('/api/compressor/manual', methods=['POST'])
def control_compressor_manual():
    """
    手动控制压缩机启停。
    需要一个JSON请求体，例如: {"start": true} 或 {"start": false}
    """
    data = request.get_json()
    if not data or 'start' not in data or not isinstance(data['start'], bool):
        return jsonify({"error": "请求体中缺少 'start' 字段或其不是一个布尔值"}), 400
    
    start = data['start']
    controller.control_compressor_manual(start)
    action = "启动" if start else "停止"
    return jsonify({"status": "success", "message": f"手动{action}压缩机命令已发送"})

@app.route('/api/compressor/auto', methods=['POST'])
def control_compressor_auto():
    """
    启用或禁用自动温控。
    需要一个JSON请求体，例如: {"enable": true} 或 {"enable": false}
    """
    data = request.get_json()
    if not data or 'enable' not in data or not isinstance(data['enable'], bool):
        return jsonify({"error": "请求体中缺少 'enable' 字段或其不是一个布尔值"}), 400
    
    enable = data['enable']
    controller.enable_auto_compressor_control(enable)
    status = "启用" if enable else "禁用"
    return jsonify({"status": "success", "message": f"自动温控已{status}"})

# --- WebSocket 事件处理 ---
@socketio.on('connect')
def handle_connect():
    """当一个客户端连接到WebSocket时，这个函数被调用。"""
    print(f"客户端已连接: {request.sid}")
    # 当新客户端连接时，立即发送一次当前状态，以便它能马上显示数据
    emit('update_status', controller.get_current_state())
    
@socketio.on('disconnect')
def handle_disconnect():
    """当客户端断开连接时调用。"""
    print(f"客户端已断开: {request.sid}")
    
@socketio.on('request_status')
def handle_request_status():
    """客户端可以主动请求一次最新状态。"""
    print(f"客户端 {request.sid} 请求了最新状态。")
    emit('update_status', controller.get_current_state())
    
# --- 提供一个简单的HTML页面用于测试 ---
@app.route('/test_ws')
def test_ws_page():
    # 为了方便，直接在这里返回HTML字符串。实际项目中会使用模板。
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>WebSocket 实时监控</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.js"></script>
    </head>
    <body>
        <h1>快递柜实时状态 (WebSocket)</h1>
        <pre id="status-data" style="background-color: #f0f0f0; padding: 10px; border-radius: 5px;"></pre>

        <script>
            // 连接到WebSocket服务器
            // 如果服务器和客户端在同一个域，可以省略URL
            const socket = io();

            socket.on('connect', () => {
                console.log('成功连接到WebSocket服务器！ ID:', socket.id);
            });

            // 监听我们自定义的 'update_status' 事件
            socket.on('update_status', (data) => {
                console.log('收到新状态:', data);
                // 将收到的JSON对象格式化后显示在页面上
                document.getElementById('status-data').textContent = JSON.stringify(data, null, 2);
            });

            socket.on('disconnect', () => {
                console.log('与服务器的连接已断开。');
            });
        </script>
    </body>
    </html>
    """

# --- 启动Web服务器 ---
if __name__ == '__main__':
    print(f" * 将在 http://{FLASK_HOST}:{FLASK_PORT} 上启动Flask-SocketIO服务器")
    socketio.run(app, host=FLASK_HOST, port=FLASK_PORT)