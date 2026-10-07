#include "main.h"
#include "apmain.h"
#include <stdio.h>
#include <string.h>

/* ---------------- 설정값 ---------------- */
#define ENCODER_COUNT     4    // 엔코더 개수
#define COUNTS_PER_STEP   2    // 노브 한 칸당 카운터 증가량 (한 칸에 2씩 바뀌면 2로 수정)
#define POLL_INTERVAL_MS  10   // 엔코더 확인 주기 (ms)
#define DEBUG_RAW         0    // 1: 카운터 원래 값도 함께 출력 (테스트용), 0: 파이로 보낼 형식만 출력

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


static void uartPrint(const char *s)
{
  HAL_UART_Transmit(&huart2, (uint8_t *)s, strlen(s), 100);
}


void apInit(void)
{
  for (int i = 0; i < ENCODER_COUNT; i++)
  {
    HAL_TIM_Encoder_Start(encTim[i], TIM_CHANNEL_ALL);          // 엔코더 모드 카운트 시작
    prevCnt[i] = (uint16_t)__HAL_TIM_GET_COUNTER(encTim[i]);    // 시작 시점 카운터 저장
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
