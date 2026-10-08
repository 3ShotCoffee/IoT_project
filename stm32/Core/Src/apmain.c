#include "main.h"
#include "apmain.h"
#include <stdio.h>
#include <string.h>

/* ---------------- 설정값 ---------------- */
#define ENCODER_COUNT     4    // 엔코더 개수
#define COUNTS_PER_STEP   2    // 노브 한 칸당 카운터 증가량 (한 칸에 2씩 바뀌면 2로 수정)
#define POLL_INTERVAL_MS  10   // 엔코더, 버튼 확인 주기 (ms)
#define DEBUG_RAW         0    // 1: 카운터 원래 값도 함께 출력 (테스트용), 0: 파이로 보낼 형식만 출력

#define BUTTON_COUNT      2    // 버튼 개수
#define DEBOUNCE_COUNT    3    // 바뀐 값이 몇 번 연속(= x 10ms) 유지되면 진짜 변화로 인정할지

/* main.c에 선언된 타이머, UART 핸들을 이 파일에서 쓰겠다는 선언 */
extern TIM_HandleTypeDef htim1;
extern TIM_HandleTypeDef htim2;
extern TIM_HandleTypeDef htim3;
extern TIM_HandleTypeDef htim4;
extern UART_HandleTypeDef huart2;

/* 엔코더 번호 순서: B1=TIM1, B2=TIM2, B3=TIM3, B4=TIM4 */
static TIM_HandleTypeDef *encTim[ENCODER_COUNT] = {&htim1, &htim2, &htim3, &htim4};
static uint16_t prevCnt[ENCODER_COUNT];   // 마지막으로 처리한 카운터 값
static uint32_t lastTick;                 // 마지막으로 확인한 시각 (ms)

/* 버튼 번호 순서: S1=PA4, S2=PB0 (CubeMX에서 GPIO_Input + Pull-up으로 설정) */
typedef struct
{
  GPIO_TypeDef *port;
  uint16_t pin;
} ButtonPin;

static const ButtonPin btnPin[BUTTON_COUNT] = {
  {GPIOA, GPIO_PIN_4},
  {GPIOB, GPIO_PIN_0},
};
static GPIO_PinState btnStable[BUTTON_COUNT];   // 채터링을 걸러낸 확정 상태 (SET=뗌, RESET=누름)
static uint8_t btnChangeCnt[BUTTON_COUNT];      // 확정 상태와 다른 값이 연속으로 몇 번 읽혔는지


static void uartPrint(const char *s)
{
  HAL_UART_Transmit(&huart2, (uint8_t *)s, strlen(s), 100);
}


static void pollEncoders(void)
{
  for (int i = 0; i < ENCODER_COUNT; i++)
  {
    uint16_t now = (uint16_t)__HAL_TIM_GET_COUNTER(encTim[i]);

    /* int16_t로 바꿔서 빼면 카운터가 65535 -> 0으로 넘어가도 올바른 차이가 나옴 */
    int16_t diff = (int16_t)(now - prevCnt[i]);
    int steps = diff / COUNTS_PER_STEP;

    if (steps != 0)
    {
      /* 처리한 칸 수만큼만 반영하고, 한 칸이 안 되는 나머지는 다음 확인 때 합산 */
      prevCnt[i] += (uint16_t)(steps * COUNTS_PER_STEP);

      char msg[40];
#if DEBUG_RAW
      snprintf(msg, sizeof(msg), "B%d:%+d (cnt=%u)\r\n", i + 1, steps, now);
#else
      snprintf(msg, sizeof(msg), "B%d:%+d\r\n", i + 1, steps);
#endif
      uartPrint(msg);
    }
  }
}


static void pollButtons(void)
{
  for (int i = 0; i < BUTTON_COUNT; i++)
  {
    GPIO_PinState raw = HAL_GPIO_ReadPin(btnPin[i].port, btnPin[i].pin);

    /* 확정 상태와 같으면 흔들림이 없는 것이므로 카운트 초기화 */
    if (raw == btnStable[i])
    {
      btnChangeCnt[i] = 0;
      continue;
    }

    /* 확정 상태와 다른 값이 DEBOUNCE_COUNT번 연속으로 읽혀야 진짜 변화로 인정
       (접점이 튕기는 동안에는 중간에 원래 값이 섞여서 카운트가 0으로 돌아감) */
    btnChangeCnt[i]++;
    if (btnChangeCnt[i] >= DEBOUNCE_COUNT)
    {
      btnStable[i] = raw;
      btnChangeCnt[i] = 0;

      /* 풀업 + 버튼이 GND 쪽이라 누르면 RESET(0) = active low */
      if (raw == GPIO_PIN_RESET)
      {
        char msg[16];
        snprintf(msg, sizeof(msg), "S%d:1\r\n", i + 1);   // 눌림 메시지
        uartPrint(msg);
      }
    }
  }
}


void apInit(void)
{
  for (int i = 0; i < ENCODER_COUNT; i++)
  {
    HAL_TIM_Encoder_Start(encTim[i], TIM_CHANNEL_ALL);          // 엔코더 모드 카운트 시작
    prevCnt[i] = (uint16_t)__HAL_TIM_GET_COUNTER(encTim[i]);    // 시작 시점 카운터 저장
  }

  for (int i = 0; i < BUTTON_COUNT; i++)
  {

    btnStable[i] = HAL_GPIO_ReadPin(btnPin[i].port, btnPin[i].pin);   // 시작 시점 버튼 상태
    btnChangeCnt[i] = 0;
  }

  lastTick = HAL_GetTick();
  uartPrint("Encoder test start\r\n");
}


void apMain(void)
{
  /* 10ms가 지나지 않았으면 아무것도 하지 않음 */
  if (HAL_GetTick() - lastTick < POLL_INTERVAL_MS)
  {
    return;
  }
  lastTick = HAL_GetTick();

  pollEncoders();
  pollButtons();
}
