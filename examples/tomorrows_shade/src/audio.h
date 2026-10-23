#ifndef AUDIO_H
#define AUDIO_H

/* The score and ambience are synthesised at start-up into one stereo buffer. */
void AudioBuild(float duration);
const float *AudioSamples(int *frameCount, int *sampleRate);
int AudioWriteWav(const char *path);
void AudioStart(float atTime);
void AudioPause(int paused);
void AudioSeek(float t);
float AudioTime(void);   /* playback position in seconds, < 0 if no audio device */
void AudioStop(void);

#endif
