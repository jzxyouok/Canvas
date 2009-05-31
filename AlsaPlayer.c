
#include "framework.h"
#include <gtk/gtk.h>
#include <asoundlib.h>
#include "clock.h"

#define F_PI 3.1415926535897932384626433832795f

bool takeAudioSource( PyObject *source, AudioSourceHolder *holder ) {
    Py_CLEAR( holder->source );
    Py_CLEAR( holder->csource );
    holder->funcs = NULL;

    if( source == NULL || source == Py_None )
        return true;

    Py_INCREF( source );
    holder->source = source;
    holder->csource = PyObject_GetAttrString( source, "_audioFrameSourceFuncs" );

    if( holder->csource == NULL ) {
        Py_CLEAR( holder->source );
        PyErr_SetString( PyExc_Exception, "The source didn't have an acceptable _audioFrameSourceFuncs attribute." );
        return false;
    }

    holder->funcs = (AudioFrameSourceFuncs*) PyCObject_AsVoidPtr( holder->csource );

    return true;
}

typedef struct {
    PyObject_HEAD

    int nextSample;
    int64_t seekTime, baseTime;
    AudioSourceHolder audioSource;
    snd_pcm_t *pcmDevice;
    GThread *playbackThread;
    GMutex *mutex;
    GCond *cond;
    bool quit, stop;
    rational rate, playSpeed;
} py_obj_AlsaPlayer;

static gpointer
playbackThread( py_obj_AlsaPlayer *self ) {
    int error;
    snd_pcm_uframes_t hwBufferSize, hwPeriodSize;

    const int bufferSize = 1024, channelCount = 2;
    float *data = malloc( bufferSize * channelCount * sizeof(float) );

    if( data == NULL ) {
        printf( "Out of memory in AlsaPlayer allocating output buffer\n" );
        return NULL;
    }

    for( ;; ) {
        g_mutex_lock( self->mutex );

        if( self->stop )
            snd_pcm_drop( self->pcmDevice );

        while( !self->quit && self->stop )
            g_cond_wait( self->cond, self->mutex );

        if( snd_pcm_state( self->pcmDevice ) == SND_PCM_STATE_SETUP )
            snd_pcm_prepare( self->pcmDevice );

        //printf( "%s\n", snd_pcm_state_name( snd_pcm_state( self->pcmDevice ) ) );

        if( self->quit ) {
            g_mutex_unlock( self->mutex );
            break;
        }

        int nextSample = self->nextSample;
        self->nextSample += bufferSize;

        snd_pcm_get_params( self->pcmDevice, &hwBufferSize, &hwPeriodSize );

        g_mutex_unlock( self->mutex );

        AudioFrame frame;
        frame.channelCount = 2;
        frame.frameData = data;
        frame.fullMinSample = nextSample;
        frame.fullMaxSample = nextSample + bufferSize - 1;
        frame.currentMinSample = frame.fullMinSample;
        frame.currentMaxSample = frame.fullMaxSample;

        self->audioSource.funcs->getFrame( self->audioSource.source, &frame );

        float *ptr = data;
        int count = bufferSize;

        while( count > 0 ) {
            g_mutex_lock( self->mutex );

            // Reset the clock so that it stays in sync
            snd_htimestamp_t tstamp;
            snd_pcm_uframes_t avail;
            snd_pcm_htimestamp( self->pcmDevice, &avail, &tstamp );

            self->baseTime = (int64_t) tstamp.tv_sec * INT64_C(1000000000) + (int64_t) tstamp.tv_nsec;
            self->seekTime = getFrameTime( &self->rate, nextSample - (hwBufferSize - avail) );

            g_mutex_unlock( self->mutex );

            error = snd_pcm_writei( self->pcmDevice, data, bufferSize );

            if( error == -EAGAIN )
                continue;

            if( error == -EPIPE ) {
                // Underrun!
                printf( "Underrun\n" );

                self->seekTime = getFrameTime( &self->rate, self->nextSample );

                snd_pcm_prepare( self->pcmDevice );
                continue;
            }

            ptr += error * frame.channelCount;
            count -= error;
        }
    }

    snd_pcm_drop( self->pcmDevice );
    free( data );

    return NULL;
}

static int
AlsaPlayer_init( py_obj_AlsaPlayer *self, PyObject *args, PyObject *kwds ) {
    PyObject *frameSource = NULL;

    if( !PyArg_ParseTuple( args, "O", &frameSource ) )
        return -1;

    if( !takeAudioSource( frameSource, &self->audioSource ) )
        return -1;

    int error;
    const char *deviceName = "default";

    if( (error = snd_pcm_open( &self->pcmDevice, "default", SND_PCM_STREAM_PLAYBACK, 0 )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Could not open PCM device %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    snd_pcm_hw_params_t *params = PyMem_Malloc( snd_pcm_hw_params_sizeof() );

    if( params == NULL ) {
        PyErr_NoMemory();
        return -1;
    }

    if( (error = snd_pcm_hw_params_any( self->pcmDevice, params )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Could not open configuration for playback on %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    if( (error = snd_pcm_hw_params_set_access( self->pcmDevice, params, SND_PCM_ACCESS_RW_INTERLEAVED )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Failed to set interleaved access on %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    // Set stereo channels for now
    if( (error = snd_pcm_hw_params_set_channels( self->pcmDevice, params, 2 )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Failed to set channel count to 2 on %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    // Grab what should be an easy-to-find format
    if( (error = snd_pcm_hw_params_set_format( self->pcmDevice, params, SND_PCM_FORMAT_FLOAT )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Failed to set sample format on %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    // Set a common sample rate
    if( (error = snd_pcm_hw_params_set_rate( self->pcmDevice, params, 48000, 0 )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Failed to set sample rate on %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    if( (error = snd_pcm_hw_params( self->pcmDevice, params )) < 0 ) {
        PyErr_Format( PyExc_Exception, "Failed to write parameter set to %s: %s", deviceName, snd_strerror( error ) );
        return -1;
    }

    PyMem_Free( params );

    //printf( "%s\n", snd_pcm_state_name( snd_pcm_state( self->pcmDevice ) ) );

    self->mutex = g_mutex_new();
    self->cond = g_cond_new();
    self->stop = true;
    self->rate.n = 48000;
    self->rate.d = 1;

    self->playbackThread = g_thread_create( (GThreadFunc) playbackThread, self, TRUE, NULL );

    return 0;
}

static void
AlsaPlayer_dealloc( py_obj_AlsaPlayer *self ) {
    if( self->mutex != NULL && self->cond != NULL ) {
        g_mutex_lock( self->mutex );
        self->quit = true;
        g_cond_signal( self->cond );
        g_mutex_unlock( self->mutex );
    }
    else
        self->quit = true;

    Py_CLEAR( self->audioSource.csource );
    Py_CLEAR( self->audioSource.source );

    if( self->playbackThread != NULL )
        g_thread_join( self->playbackThread );

    if( self->pcmDevice != NULL ) {
        snd_pcm_close( self->pcmDevice );
        self->pcmDevice = NULL;
    }

    if( self->mutex != NULL ) {
        g_mutex_free( self->mutex );
        self->mutex = NULL;
    }

    if( self->cond != NULL ) {
        g_cond_free( self->cond );
        self->cond = NULL;
    }

    self->ob_type->tp_free( (PyObject*) self );
}

static int64_t _pcmTime( py_obj_AlsaPlayer *self ) {
    snd_pcm_uframes_t avail;
    snd_htimestamp_t tstamp;

    snd_pcm_htimestamp( self->pcmDevice, &avail, &tstamp );

    return (int64_t) tstamp.tv_sec * INT64_C(1000000000) + (int64_t) tstamp.tv_nsec;
}

static int64_t
_getPresentationTime_nolock( py_obj_AlsaPlayer *self ) {
    if( self->stop )
        return self->seekTime;

    int64_t elapsed = (_pcmTime( self ) - self->baseTime) * self->playSpeed.n;
    int64_t seekTime = self->seekTime;
    unsigned int d = self->playSpeed.d;

    if( d == 1 )
        return elapsed + seekTime;
    else
        return elapsed / d + seekTime;
}

static int64_t
_getPresentationTime( py_obj_AlsaPlayer *self ) {
    g_mutex_lock( self->mutex );
    int64_t result = _getPresentationTime_nolock( self );
    g_mutex_unlock( self->mutex );

    return result;
}

static void
_getSpeed( py_obj_AlsaPlayer *self, rational *result ) {
    g_mutex_lock( self->mutex );
    *result = self->playSpeed;
    g_mutex_unlock( self->mutex );
}

static void
_set( py_obj_AlsaPlayer *self, int64_t seekTime, rational *speed ) {
    g_mutex_lock( self->mutex );
    self->baseTime = _pcmTime( self );
    printf( "%lld\n", self->baseTime );
    self->seekTime = seekTime;
    self->playSpeed = *speed;
    self->nextSample = getTimeFrame( &self->rate, self->seekTime );
    self->seekTime = getFrameTime( &self->rate, self->nextSample );
    g_mutex_unlock( self->mutex );
}

static PyObject *
AlsaPlayer_set( py_obj_AlsaPlayer *self, PyObject *args ) {
    PyObject *rateObj;
    rational rate;
    int64_t time;

    if( !PyArg_ParseTuple( args, "OL", &rateObj, &time ) )
        return NULL;

    if( !parseRational( rateObj, &rate ) )
        return NULL;

    _set( self, time, &rate );

    Py_RETURN_NONE;
}

static PresentationClockFuncs sourceFuncs = {
    .getPresentationTime = (clock_getPresentationTimeFunc) _getPresentationTime,
    .getSpeed = (clock_getSpeedFunc) _getSpeed
};

static PyObject *pysourceFuncs;

static PyObject *
AlsaPlayer_getFuncs( py_obj_AlsaPlayer *self, void *closure ) {
    return pysourceFuncs;
}

static PyGetSetDef AlsaPlayer_getsetters[] = {
    { "_presentationClockFuncs", (getter) AlsaPlayer_getFuncs, NULL, "Presentation clock C API." },
    { NULL }
};

static PyObject *
AlsaPlayer_getPresentationTime( py_obj_AlsaPlayer *self ) {
    return Py_BuildValue( "L", _getPresentationTime( self ) );
}

static PyObject *
AlsaPlayer_stop( py_obj_AlsaPlayer *self ) {
    g_mutex_lock( self->mutex );
    self->seekTime = _getPresentationTime_nolock( self );
    self->stop = true;
    self->playSpeed.n = 0;
    self->playSpeed.d = 1;

    self->nextSample = getTimeFrame( &self->rate, self->seekTime );
    self->seekTime = getFrameTime( &self->rate, self->nextSample );

    g_mutex_unlock( self->mutex );

    Py_RETURN_NONE;
}

static PyObject *
AlsaPlayer_play( py_obj_AlsaPlayer *self, PyObject *args ) {
    PyObject *rateObj;
    rational rate;

    if( !PyArg_ParseTuple( args, "O", &rateObj ) )
        return NULL;

    if( !parseRational( rateObj, &rate ) )
        return NULL;

    g_mutex_lock( self->mutex );
    self->stop = false;

    self->baseTime = _pcmTime( self );
    self->playSpeed = rate;
    g_cond_signal( self->cond );
    g_mutex_unlock( self->mutex );

    Py_RETURN_NONE;
}

static PyObject *
AlsaPlayer_seek( py_obj_AlsaPlayer *self, PyObject *args ) {
    int64_t time;

    if( !PyArg_ParseTuple( args, "L", &time ) )
        return NULL;

    _set( self, time, &self->playSpeed );

    Py_RETURN_NONE;
}

static PyMethodDef AlsaPlayer_methods[] = {
    { "set", (PyCFunction) AlsaPlayer_set, METH_VARARGS,
        "Sets the speed and current time." },
    { "seek", (PyCFunction) AlsaPlayer_seek, METH_VARARGS,
        "Sets the current time." },
    { "play", (PyCFunction) AlsaPlayer_play, METH_VARARGS,
        "Plays audio from the source starting at the current spot." },
    { "stop", (PyCFunction) AlsaPlayer_stop, METH_NOARGS,
        "Stops playing audio from the source." },
    { "getPresentationTime", (PyCFunction) AlsaPlayer_getPresentationTime, METH_NOARGS,
        "Gets the current presentation time in nanoseconds." },
    { NULL }
};

static PyTypeObject py_type_AlsaPlayer = {
    PyObject_HEAD_INIT(NULL)
    0,            // ob_size
    "fluggo.video.AlsaPlayer",    // tp_name
    sizeof(py_obj_AlsaPlayer),    // tp_basicsize
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_dealloc = (destructor) AlsaPlayer_dealloc,
    .tp_init = (initproc) AlsaPlayer_init,
    .tp_methods = AlsaPlayer_methods,
    .tp_getset = AlsaPlayer_getsetters
};

NOEXPORT void init_AlsaPlayer( PyObject *module ) {
    if( PyType_Ready( &py_type_AlsaPlayer ) < 0 )
        return;

    Py_INCREF( &py_type_AlsaPlayer );
    PyModule_AddObject( module, "AlsaPlayer", (PyObject *) &py_type_AlsaPlayer );

    pysourceFuncs = PyCObject_FromVoidPtr( &sourceFuncs, NULL );
}
