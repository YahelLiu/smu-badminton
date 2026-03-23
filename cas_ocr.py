import cv2
import numpy as np
import ncnn
from PIL import Image

# 配置参数（可根据实际模型调整）
MEAN_VALUES = [123.675, 116.28, 103.53]
NORM_VALUES = [1/58.395, 1/57.12, 1/57.375]
EQUAL_SYMBOL_KEY_START = 0.7
EQUAL_SYMBOL_KEY_END = 1.0
KEY_POINT_SYMBOL = [0.25, 0.58, 0.75]
KEY_POINT_CHS = [0.15, 0.33, 0.46]
CONFIG_THRESH = 200

# 加载NCNN模型

def load_ncnn_model(param_path, bin_path):
    net = ncnn.Net()
    net.load_param(param_path)
    net.load_model(bin_path)
    return net

# 修改为从'model/'目录加载
MODEL_DIR = 'model/'
digit_net = load_ncnn_model(MODEL_DIR + 'resnet34_digit_latest.fp32.param', MODEL_DIR + 'resnet34_digit_latest.fp32.bin')
operator_net = load_ncnn_model(MODEL_DIR + 'resnet18_operator_latest.fp32.param', MODEL_DIR + 'resnet18_operator_latest.fp32.bin')
equal_symbol_net = load_ncnn_model(MODEL_DIR + 'resnet18_equal_symbol_latest.fp32.param', MODEL_DIR + 'resnet18_equal_symbol_latest.fp32.bin')

def split_img_by_ratio(image, start_ratio, end_ratio):
    h, w = image.shape[:2]
    if start_ratio > end_ratio:
        start_ratio, end_ratio = end_ratio, start_ratio
    x1 = int(w * start_ratio)
    x2 = int(w * end_ratio)
    if end_ratio >= 1:
        x2 = w
    return image[:, x1:x2].copy()

def preprocess_img(img_input):
    """
    预处理图片
    img_input: 可以是文件路径(str)或图片字节数据(bytes)
    """
    if isinstance(img_input, str):
        # 文件路径
        img = cv2.imread(img_input)
    elif isinstance(img_input, bytes):
        # 字节数据
        img_array = np.frombuffer(img_input, dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    else:
        raise ValueError("img_input must be str (file path) or bytes")
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, CONFIG_THRESH, 255, cv2.THRESH_BINARY)
    # 转为3通道
    image = cv2.merge([binary, binary, binary])
    return image

def ncnn_predict(net, img):
    img = cv2.resize(img, (224, 224))
    # ncnn 只支持BGR uint8输入
    mat = ncnn.Mat.from_pixels(img, ncnn.Mat.PixelType.PIXEL_BGR, 224, 224)
    mean = np.array(MEAN_VALUES, dtype=np.float32)
    norm = np.array(NORM_VALUES, dtype=np.float32)
    mat.substract_mean_normalize(mean, norm)
    ex = net.create_extractor()
    ex.input("input", mat)
    out = ncnn.Mat()
    ex.extract("output", out)
    out_np = np.array(out)
    return int(np.argmax(out_np))

def predict_by_model(model, img):
    return ncnn_predict(model, img)

def get_operator_str_by_int(type_):
    if type_ in [0, 1]:
        return "+"
    elif type_ in [2, 3]:
        return "-"
    elif type_ in [4, 5]:
        return "*"
    else:
        return ""

def calculate_operator(left, right, operator_type):
    if operator_type in [0, 1]:
        return left + right
    elif operator_type in [2, 3]:
        return left - right
    elif operator_type in [4, 5]:
        return left * right
    else:
        return 0
def draw_split_lines_on_image(image, key_points, color=(0, 0, 255), thickness=2):
    h, w = image.shape[:2]
    img_copy = image.copy()
    for x_ratio in key_points:
        x = int(w * x_ratio)
        cv2.line(img_copy, (x, 0), (x, h), color, thickness)
    return img_copy
def predict_validate_code(img_input):
    """
    识别验证码
    img_input: 可以是文件路径(str)或图片字节数据(bytes)
    """
    image = preprocess_img(img_input)
    h, w = image.shape[:2]
    # 1. 先识别等号类型
    image_equal_symbol = split_img_by_ratio(image, EQUAL_SYMBOL_KEY_START, EQUAL_SYMBOL_KEY_END)
    predicted_equal_symbol = predict_by_model(equal_symbol_net, image_equal_symbol)
    # 2. 根据等号类型选择分割点
    if predicted_equal_symbol == 0:
        # 等号是中文，直接用 KEY_POINT_CHS
        key_point = KEY_POINT_CHS
        image_digit_1 = split_img_by_ratio(image, 0, key_point[0])
        img_operator = split_img_by_ratio(image, key_point[0], key_point[1])
        image_digit_2 = split_img_by_ratio(image, key_point[1], key_point[2])
    else:
        # 等号是符号
        key_point = KEY_POINT_SYMBOL
        image_digit_1 = split_img_by_ratio(image, 0, key_point[0])
        img_operator = split_img_by_ratio(image, key_point[0], key_point[1])
        image_digit_2 = split_img_by_ratio(image, key_point[1], key_point[2])

    # 4. 识别
    predicted_operator = predict_by_model(operator_net, img_operator)
    predicted_digit_1 = predict_by_model(digit_net, image_digit_1)
    predicted_digit_2 = predict_by_model(digit_net, image_digit_2)
    # 5. 计算结果
    result = calculate_operator(predicted_digit_1, predicted_digit_2, predicted_operator)
    expr = f"{predicted_digit_1} {get_operator_str_by_int(predicted_operator)} {predicted_digit_2} = {result}"
    return result, expr, predicted_equal_symbol, predicted_operator, predicted_digit_1, predicted_digit_2

if __name__ == '__main__':
    # 测试
    img_path = r'captcha\captcha_1752206184778.jpg'
    result, expr, *_ = predict_validate_code(img_path)
    print("识别表达式：", expr)