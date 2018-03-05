import smbus as I2C
import RPi.GPIO as GPIO
import time
from enum import IntEnum   # для создания нумерованных списков
import math
import threading

###
'''
Класс для получения информации от одноканального АЦП MCP3221.
При инициализации задаются значения: vRef - опорное напряжение (относительно которого происходит измерение),
gain - коэффициент делителя напряжения (если он есть.
Методы:
Read - читает информацию из шины I2C (2 байта измерения);
GetVoltage - Вызывает метод Read, преобразует полученное значение в напряжение исходя из заданного опорного напряжения;
GetBattery - Вызывает метод GetVoltage, домножает полученное напряжение на коэффициент делителя напряжения.
'''
###


class Battery(threading.Thread):
    def __init__(self, vRef=3.3, gain=7.66):
        threading.Thread.__init__(self)
        self._addr = 0x4D
        self._vRef = vRef
        self._gain = gain
        self._i2c = I2C.SMBus(1)
        threading.Thread.__init__(self)
        self._exit = False  # флаг завершения тредов
        self._filteredVoltage = 0   # отфильтрованное значение напряжения
        self._K = 0.1   # коэффициент фильтрации

    def run(self):
        while not self._exit:    # 20 раз в секунду опрашивает АЦП, фильтрует значение
            self._filteredVoltage = self._filteredVoltage * (1 - self._K) + self.GetVoltageInstant() * self._K
            time.sleep(0.05)

    def _ReadRaw(self):    # чтение показаний АЦП
        reading = self._i2c.read_i2c_block_data(self._addr, 0x00, 2)
        return (reading[0] << 8) + reading[1]

    def _ReadConverted(self):   # преобразование к напряжению, относительно опорного (после предделителя)
        voltage = (self._ReadRaw() / 4095) * self._vRef  # 4095 - число разрядов АЦП
        return voltage

    def GetVoltageInstant(self):  # возвращает моментальное значение напряжения аккумулятора с АЦП (до предделителя)
        battery = self._ReadConverted() * self._gain
        return round(battery, 2)

    def Stop(self):     # останавливает треды
        self._exit = True

    def GetVoltageFiltered(self):   # возвращаяет отфильтрованное значение напряжения
        return round(self._filteredVoltage, 2)

    def Calibrate(self, exactVoltage):  # подгоняет коэффциент делителя напряжения
        value = 0
        for i in range(100):
            value += self._ReadConverted()
            time.sleep(0.01)
        value /= 100
        self._gain = exactVoltage/value
    # TODO: возможно сделать калибровку более точной (но вроде как без нее все работает и так)


###
'''
Класс для работы с миркосхемой, генерирующей ШИМ сигналы.
Позволяет задавать ШИМ сигнал отдельно для каждого канала. Канал требует инициализации.
Возможные значения для инициализации канала (берутся из соответствующего нумерованного списка):
servo90 - для серв с углом поворота 90 градусов;
servo180 - для серв с углом поворота 180 градусов;
servo270 - для серв с углом поворота 270 градусов;
reverseMotor - для подключения драйвера моторов, у которого крайние значения отвечают за разное направление вращения моторов.
forwardMotor - для подключения драйвера моторов, у которого только одно направление вращения (от 0 до 100)
'''
###

# Регистры для работы с PCA9685
_PCA9685_ADDRESS = 0x40
_MODE1 = 0x00
_MODE2 = 0x01
_SUBADR1 = 0x02
_SUBADR2 = 0x03
_SUBADR3 = 0x04
_PRESCALE = 0xFE
_LED0_ON_L = 0x06
_LED0_ON_H = 0x07
_LED0_OFF_L = 0x08
_LED0_OFF_H = 0x09
_ALL_LED_ON_L = 0xFA
_ALL_LED_ON_H = 0xFB
_ALL_LED_OFF_L = 0xFC
_ALL_LED_OFF_H = 0xFD

# Биты для работы с PCA9685:
_RESTART = 0x80     # при чтении возвращает свое состояние, при записи - разрешает или запрещает перезагрузку
_SLEEP = 0x10       # режим энергосбережения (выключен внутренний осциллятор)
_ALLCALL = 0x01     # PCA9685 будет отвечать на запрос всех устройств на шине
_INVRT = 0x10       # инверсный или неинверсный выход сигнала на микросхеме
_OUTDRV = 0x04      # способ подключения светодиодов (см. даташит, нам это вроде не надо)

# при частоте ШИМ 50 Гц (20 мс) получаем
_min = 205  # 1 мс (~ 4096/20)
_max = 410  # 2 мс (~ 4096*2/20)


class PwmMode(IntEnum):     # список режимов работы
    servo90 = 90            # серва 90 градусов
    servo180 = 180          # серва 180 градусов
    servo270 = 270          # серва 270 градусов
    forwardMotor = 100      # мотор без реверса
    reverseMotor = 4        # мотор с реверсом
    onOff = 5               # вкл/выкл пина


class Pwm:
    def __init__(self):
        self._i2c = I2C.SMBus(1)
        # инициализируем микросхему
        self._Write8(_MODE2, _OUTDRV)
        self._Write8(_MODE1, _ALLCALL)
        time.sleep(0.005)
        mode1 = self._ReadU8(_MODE1)    # читаем установленный режим
        mode1 = mode1 & ~_SLEEP     # будим
        self._Write8(_MODE1, mode1)
        time.sleep(0.005)
        self._SetPwmFreq(50)    # устанавливаем частоту сигнала 50 Гц
        # словарь, содержащий номера каналов и выставленный режим
        self._channel = {}

    def _Write8(self, register, value):  # запись 8битного значения в заданный регистр
        value = value & 0xFF
        self._i2c.write_byte_data(_PCA9685_ADDRESS, register, value)

    def _ReadU8(self, register):    # чтение unsigned byte
        result = self._i2c.read_byte_data(_PCA9685_ADDRESS, register) & 0xFF
        return result

    def _SetPwmFreq(self, freqHz):  # устанавливает частоту ШИМ сигнала в Гц
        prescaleval = 25000000.0    # 25MHz
        prescaleval /= 4096.0       # 12-bit
        prescaleval /= freqHz
        prescaleval -= 1
        prescale = int(math.floor(prescaleval + 0.5))
        oldmode = self._ReadU8(_MODE1)  # смотрим какой режим был у микросхемы
        newmode = (oldmode & 0x7F) | 0x10   # отключаем внутреннее тактирование, чтобы внести изменения
        self._Write8(_MODE1, newmode)
        self._Write8(_PRESCALE, prescale)    # изменяем частоту
        self._Write8(_MODE1, oldmode)    # включаем тактирование обратно
        time.sleep(0.005)   # ждем пока оно включится
        self._Write8(_MODE1, oldmode | 0x08)     # разрешаем микросхеме отвечать на subaddress 1

    def _SetPwm(self, channel, value):  # установка значения для канала
        self._Write8(_LED0_ON_L + 4 * channel, 0 & 0xFF)  # момент включения в цикле
        self._Write8(_LED0_ON_H + 4 * channel, 0 >> 8)
        self._Write8(_LED0_OFF_L + 4 * channel, value & 0xFF)  # момент выключения в цикле
        self._Write8(_LED0_OFF_H + 4 * channel, value >> 8)

    def Reset(self):    # программный сброс микросхемы
        val = 0x06 & 0xFF
        self._i2c.write_byte(_PCA9685_ADDRESS, val)

    def InitChannel(self, channel, value):  # инициализация канала в определенном режиме
        if 0 <= channel <= 15:
            self._channel[channel] = value
        else:
            raise ValueError("Channel number must be from 0 to 15 (inclusive).")

    def SetChannel(self, channel, value):   # установка значения канала в зависимости от режима
        try:
            mode = self._channel[channel]
        except KeyError:
            raise ValueError("Channel haven't been inited!")
        # если канал установлен на режим работы "в одну сторону" - сервы или мотор без реверса
        if mode == PwmMode.reverseMotor:  # если говорим о моторе с реверсом
            if value < -100:    # обрезаем диапазон
                value = -100
            if value > 100:
                value = 100
            value += 100    # сдвигаем диапазон -100-100 -> 0-200
            value *= 205/200    # чуть изменяем 0-200 -> 0-205
            value += 205    # сдвигаем 0-205 -> 205-410
        elif mode == PwmMode.onOff:
            if value < 0:
                raise ValueError("Value must be True or False for On/Off mode")
            if value is True:   # если надо включить (True) - зажигаем полностью
                value = 4095
            else:               # иначе выключаем
                value = 0
        else:
            if value < 0:   # обрезаем крайние значения
                value = 0
            if value > mode.value:
                value = mode.value
            value *= 205/mode.value     # изменяем диапазон 0-mode -> 0-205
            value += 205        # сдвигаем диапазон 0-205 -> 205-410
        # то нужно устанавливать значение от 0 до максимального, задаваемого режимом
        self._SetPwm(channel, int(value))  # устанавливаем значение
    # TODO: убедиться с разными сервами, что все работает. Возможно добавить режим торможения для мотора с реверсом.


###
'''
Классы для работы с дисплеем.
'''
###
# Регистры для работы с SSD1306
_SSD1306_I2C_ADDRESS = 0x3C    # 011110+SA0+RW - 0x3C or 0x3D
_SSD1306_SETCONTRAST = 0x81
_SSD1306_DISPLAYALLON_RESUME = 0xA4
_SSD1306_DISPLAYALLON = 0xA5
_SSD1306_NORMALDISPLAY = 0xA6
_SSD1306_INVERTDISPLAY = 0xA7
_SSD1306_DISPLAYOFF = 0xAE
_SSD1306_DISPLAYON = 0xAF
_SSD1306_SETDISPLAYOFFSET = 0xD3
_SSD1306_SETCOMPINS = 0xDA
_SSD1306_SETVCOMDETECT = 0xDB
_SSD1306_SETDISPLAYCLOCKDIV = 0xD5
_SSD1306_SETPRECHARGE = 0xD9
_SSD1306_SETMULTIPLEX = 0xA8
_SSD1306_SETLOWCOLUMN = 0x00
_SSD1306_SETHIGHCOLUMN = 0x10
_SSD1306_SETSTARTLINE = 0x40
_SSD1306_MEMORYMODE = 0x20
_SSD1306_COLUMNADDR = 0x21
_SSD1306_PAGEADDR = 0x22
_SSD1306_COMSCANINC = 0xC0
_SSD1306_COMSCANDEC = 0xC8
_SSD1306_SEGREMAP = 0xA0
_SSD1306_CHARGEPUMP = 0x8D
_SSD1306_EXTERNALVCC = 0x1
_SSD1306_SWITCHCAPVCC = 0x2
# Константы для работы с прокруткой дисплея
_SSD1306_ACTIVATE_SCROLL = 0x2F
_SSD1306_DEACTIVATE_SCROLL = 0x2E
_SSD1306_SET_VERTICAL_SCROLL_AREA = 0xA3
_SSD1306_RIGHT_HORIZONTAL_SCROLL = 0x26
_SSD1306_LEFT_HORIZONTAL_SCROLL = 0x27
_SSD1306_VERTICAL_AND_RIGHT_HORIZONTAL_SCROLL = 0x29
_SSD1306_VERTICAL_AND_LEFT_HORIZONTAL_SCROLL = 0x2A


class SSD1306Base(object):  # Базовый класс для работы с OLED дисплеями на базе SSD1306
    def __init__(self, width, height, i2cBus=None, i2cAddr=_SSD1306_I2C_ADDRESS):
        self.width = width  # ширина и высота дисплея
        self.height = height
        self._pages = height//8     # строки дисплея
        self._buffer = [0]*(width*self._pages)  # буффер изображения (из нулей)
        if i2cBus is None:
            self._i2c = I2C.SMBus(1)
        else:
            self._i2c = i2cBus

    def _Initialize(self):
        raise NotImplementedError

    def Command(self, c):  # Отправка байта команды дисплею
        control = 0x00
        self._Write8(control, c)

    def Data(self, c):  # Отправка байта данных дисплею
        control = 0x40
        self._Write8(control, c)

    def Begin(self, vccstate=_SSD1306_SWITCHCAPVCC):    # инициализация дисплея
        self._vccstate = vccstate
        self._Initialize()
        self.Command(_SSD1306_DISPLAYON)

    def Display(self):  # вывод программного буффера дисплея на физическое устройство
        self.Command(_SSD1306_COLUMNADDR)   # задаем нумерацию столбцов
        self.Command(0)                     # Начало столбцов (0 = сброс)
        self.Command(self.width-1)          # адрес последнего столбца
        self.Command(_SSD1306_PAGEADDR)     # задаем адрес страниц (строк)
        self.Command(0)                     # Начало строк (0 = сброс)
        self.Command(self._pages-1)         # адрес последней строки
        # Выводим буффер данных
        for i in range(0, len(self._buffer), 16):
            control = 0x40
            self._WriteList(control, self._buffer[i:i+16])

    def Image(self, image):     # выводит картинку созданную при помощи библиотеки PIL
        # картинка должна быть в режиме mode = 1 и совпадать по размеру с дисплеем
        if image.mode != '1':
            raise ValueError('Image must be in mode 1.')
        imWidth, imHeight = image.size
        if imWidth != self.width or imHeight != self.height:
            raise ValueError('Image must be same dimensions as display ({0}x{1})'.format(self.width, self.height))
        pix = image.load()  # выгружаем пиксели из картинки
        # проходим через память чтобы записать картинку в буффер
        index = 0
        for page in range(self._pages):
            # идем по оси x (колонны)
            for x in range(self.width):
                bits = 0
                for bit in [0, 1, 2, 3, 4, 5, 6, 7]:    # быстрее чем range
                    bits = bits << 1
                    bits |= 0 if pix[(x, page*8 + 7 - bit)] == 0 else 1
                # обновляем буффер и увеличиваем счетчик
                self._buffer[index] = bits
                index += 1

    def Clear(self):    # очищает буффер изображения
        self._buffer = [0]*(self.width*self._pages)

    def SetContrast(self, contrast):    # установка контрастности дисплея от 0 до 255
        if contrast < 0 or contrast > 255:
            raise ValueError('Contrast must be value from 0 to 255 (inclusive).')
        self.Command(_SSD1306_SETCONTRAST)
        self.Command(contrast)

    def Dim(self, dim):     # корректировка контрастности дисплея (пока не очень понял как оно работает!)
        contrast = 0
        if not dim:
            if self._vccstate == _SSD1306_EXTERNALVCC:
                contrast = 0x9F
            else:
                contrast = 0xCF

    def _Write8(self, register, value):  # запись 8битного значения в заданный регистр
        value = value & 0xFF
        self._i2c.write_byte_data(_SSD1306_I2C_ADDRESS, register, value)

    def _WriteList(self, register, data):   # запись списка байтов в заданный регистр
        for i in range(len(data)):
            self._i2c.write_byte_data(_SSD1306_I2C_ADDRESS, register, data[i])

    def _ReadU8(self, register):    # чтение unsigned byte
        result = self._i2c.read_byte_data(_SSD1306_I2C_ADDRESS, register) & 0xFF
        return result


class SSD1306_128_64(SSD1306Base):  # класс для дисплея 128*64 pix
    def __init__(self, i2cBus=None, i2cAddr=_SSD1306_I2C_ADDRESS):
        # вызываем конструктор класса
        super(SSD1306_128_64, self).__init__(128, 64, i2cBus, i2cAddr)

    def _Initialize(self):
        # инициализация конкретно для размера 128x64
        self.Command(_SSD1306_DISPLAYOFF)           # 0xAE
        self.Command(_SSD1306_SETDISPLAYCLOCKDIV)   # 0xD5
        self.Command(0x80)                          # предлагаемоое соотношение 0x80
        self.Command(_SSD1306_SETMULTIPLEX)         # 0xA8
        self.Command(0x3F)
        self.Command(_SSD1306_SETDISPLAYOFFSET)     # 0xD3
        self.Command(0x0)                           # без отступов
        self.Command(_SSD1306_SETSTARTLINE | 0x0)   # начинаем строки с 0
        self.Command(_SSD1306_CHARGEPUMP)           # 0x8D
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x10)
        else:
            self.Command(0x14)
        self.Command(_SSD1306_MEMORYMODE)           # 0x20
        self.Command(0x00)                          # иначе работает неправильно (0x0 act like ks0108)
        self.Command(_SSD1306_SEGREMAP | 0x1)
        self.Command(_SSD1306_COMSCANDEC)
        self.Command(_SSD1306_SETCOMPINS)           # 0xDA
        self.Command(0x12)
        self.Command(_SSD1306_SETCONTRAST)          # 0x81
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x9F)
        else:
            self.Command(0xCF)
        self.Command(_SSD1306_SETPRECHARGE)         # 0xd9
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x22)
        else:
            self.Command(0xF1)
        self.Command(_SSD1306_SETVCOMDETECT)        # 0xDB
        self.Command(0x40)
        self.Command(_SSD1306_DISPLAYALLON_RESUME)  # 0xA4
        self.Command(_SSD1306_NORMALDISPLAY)        # 0xA6


class SSD1306_128_32(SSD1306Base):  # класс для дисплея 128*32 pix
    def __init__(self, i2cBus=None, i2cAddr=_SSD1306_I2C_ADDRESS):
        # Вызываем конструктор класса
        super(SSD1306_128_32, self).__init__(128, 32, i2cBus, i2cAddr)

    def _Initialize(self):
        # Инициализация конкретно для размера 128x32 pix
        self.Command(_SSD1306_DISPLAYOFF)           # 0xAE
        self.Command(_SSD1306_SETDISPLAYCLOCKDIV)   # 0xD5
        self.Command(0x80)                          # предлагаемоое соотношение 0x80
        self.Command(_SSD1306_SETMULTIPLEX)         # 0xA8
        self.Command(0x1F)
        self.Command(_SSD1306_SETDISPLAYOFFSET)     # 0xD3
        self.Command(0x0)                           # без отступов
        self.Command(_SSD1306_SETSTARTLINE | 0x0)   # начинаем строки с 0
        self.Command(_SSD1306_CHARGEPUMP)           # 0x8D
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x10)
        else:
            self.Command(0x14)
        self.Command(_SSD1306_MEMORYMODE)           # 0x20
        self.Command(0x00)                          # иначе работает неправильно (0x0 act like ks0108)
        self.Command(_SSD1306_SEGREMAP | 0x1)
        self.Command(_SSD1306_COMSCANDEC)
        self.Command(_SSD1306_SETCOMPINS)           # 0xDA
        self.Command(0x02)
        self.Command(_SSD1306_SETCONTRAST)          # 0x81
        self.Command(0x8F)
        self.Command(_SSD1306_SETPRECHARGE)         # 0xd9
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x22)
        else:
            self.Command(0xF1)
        self.Command(_SSD1306_SETVCOMDETECT)        # 0xDB
        self.Command(0x40)
        self.Command(_SSD1306_DISPLAYALLON_RESUME)  # 0xA4
        self.Command(_SSD1306_NORMALDISPLAY)        # 0xA6


class SSD1306_96_16(SSD1306Base):
    def __init__(self, i2cBus=None, i2cAddr=_SSD1306_I2C_ADDRESS):
        # Вызываем конструктор класса
        super(SSD1306_96_16, self).__init__(96, 16, i2cBus, i2cAddr)

    def _Initialize(self):
        # Инициализация конкретно для размера 96x16 pix
        self.Command(_SSD1306_DISPLAYOFF)           # 0xAE
        self.Command(_SSD1306_SETDISPLAYCLOCKDIV)   # 0xD5
        self.Command(0x60)                          # предлагаемоое соотношение 0x60
        self.Command(_SSD1306_SETMULTIPLEX)         # 0xA8
        self.Command(0x0F)
        self.Command(_SSD1306_SETDISPLAYOFFSET)     # 0xD3
        self.Command(0x0)                           # без отступов
        self.Command(_SSD1306_SETSTARTLINE | 0x0)   # начинаем строки с 0
        self.Command(_SSD1306_CHARGEPUMP)           # 0x8D
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x10)
        else:
            self.Command(0x14)
        self.Command(_SSD1306_MEMORYMODE)           # 0x20
        self.Command(0x00)                          # иначе работает неправильно (0x0 act like ks0108)
        self.Command(_SSD1306_SEGREMAP | 0x1)
        self.Command(_SSD1306_COMSCANDEC)
        self.Command(_SSD1306_SETCOMPINS)           # 0xDA
        self.Command(0x02)
        self.Command(_SSD1306_SETCONTRAST)          # 0x81
        self.Command(0x8F)
        self.Command(_SSD1306_SETPRECHARGE)         # 0xd9
        if self._vccstate == _SSD1306_EXTERNALVCC:
            self.Command(0x22)
        else:
            self.Command(0xF1)
        self.Command(_SSD1306_SETVCOMDETECT)        # 0xDB
        self.Command(0x40)
        self.Command(_SSD1306_DISPLAYALLON_RESUME)  # 0xA4
        self.Command(_SSD1306_NORMALDISPLAY)        # 0xA6


###
'''
Класс для работы с кнопкой и светодиодом.
При создании класса инициализируются пины.
'''
###
_chanButton = 20
_chanLed = 21


class Gpio:
    def __init__(self):   # флаг, по которому будем очищать (или нет) GPIO
        GPIO.setwarnings(False)  # очищаем, если кто-то еще использовал GPIO воизбежание ошибок
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(_chanButton, GPIO.IN, pull_up_down = GPIO.PUD_OFF)
        GPIO.setup(_chanLed, GPIO.OUT, initial=GPIO.LOW)

    # добавление функции, которая срабатывает при нажатии на кнопку, у функции обязательно должен быть один аргумент, который ему передает GPIO (см. пример)
    def ButtonAddEvent(self, foo):
        if foo is not None:
            GPIO.add_event_detect(20, GPIO.FALLING, callback = foo, bouncetime = 200)

    def LedSet(self, value):    # включает или выключает светодиод в зависимости от заданного значения
        GPIO.output(_chanLed, value)

    def LedToggle(self):    # переключает состояние светодиода
        GPIO.output(_chanLed, not GPIO.input(_chanLed))

    def CleanUp(self):
        GPIO.cleanup()
