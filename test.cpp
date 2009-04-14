
#include "framework.h"
#include "clock.h"

#include <gtk/gtk.h>
#include <gtk/gtkgl.h>
#include <gdk/gdkkeysyms.h>
#include <GL/glew.h>
#include <GL/gl.h>

#include <sstream>
#include <string>
#include <stdlib.h>
#include <semaphore.h>
#include <unistd.h>
#include <time.h>
#include <sys/timerfd.h>

using namespace Iex;
using namespace Imf;


//static float gamma22Func( float input ) {
//    return powf( input, 2.2 );
//}

// (16 / 255) ^ 2.2
const float __gamma22Base = 0.002262953f;

// (235 / 255) ^ 2.2 - __gamma22Base
const float __gamma22Extent = 0.835527791f - __gamma22Base;
const float __gamma22Fixer = 1.0f / __gamma22Extent;
const float __gammaCutoff = 21.0f / 255.0f;

inline float clamppowf( float x, float y ) {
    if( x < 0.0f )
        return 0.0f;

    if( x > 1.0f )
        return 1.0f;

    return powf( x, y );
}

inline float clampf( float x ) {
    return (x < 0.0f) ? 0.0f : ((x > 1.0f) ? 1.0f : x);
}

static float gamma45Func( float input ) {
    return Imath::clamp( powf( input, 0.45f ) * 255.0f, 0.0f, 255.0f );
}

static halfFunction<half> __gamma45( gamma45Func, half( -256.0f ), half( 256.0f ) );

static void checkGLError() {
    int error = glGetError();

    switch( error ) {
        case GL_NO_ERROR:
            return;

        case GL_INVALID_OPERATION:
            puts( "Invalid operation" );
            return;

        case GL_INVALID_VALUE:
            puts( "Invalid value" );
            return;

        case GL_INVALID_ENUM:
            puts( "Invalid enum" );
            return;

        default:
            puts( "Other GL error" );
            return;
    }
}

static void drawFrame( void *rgb, int width, int height, float pixelAspectRatio ) {
    width = (int)(width * pixelAspectRatio);

    glLoadIdentity();
    glViewport( 0, 0, width, height );
    glOrtho( 0, width, height, 0, -1, 1 );

    glClearColor( 0.0f, 1.0f, 0.0f, 1.0f );
    glClear( GL_COLOR_BUFFER_BIT );

#if 1
    glPixelStorei( GL_UNPACK_ALIGNMENT, 1 );
    glTexParameteri( GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR );
    glTexParameteri( GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR );
    glTexParameteri( GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE );
    glTexParameteri( GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE );
    glTexImage2D( GL_TEXTURE_2D, 0, GL_RGB, 720, 480,
        0, GL_RGB, GL_UNSIGNED_BYTE, rgb );
    checkGLError();

    glEnable( GL_TEXTURE_2D );

    glBegin( GL_QUADS );
    glTexCoord2f( 0, 1 );
    glVertex2i( 0, 0 );
    glTexCoord2f( 1, 1 );
    glVertex2i( width, 0 );
    glTexCoord2f( 1, 0 );
    glVertex2i( width, height );
    glTexCoord2f( 0, 0 );
    glVertex2i( 0, height );
    glEnd();

    glDisable( GL_TEXTURE_2D );
#else
    glRasterPos2i( 0, height );
    glPixelZoom( pixelAspectRatio, 1.0f );

    if( GLEW_ARB_multisample ) {
        int w;
        glGetIntegerv( GL_SAMPLE_BUFFERS_ARB, &w );
        printf( "%d\n", w );
        glEnable( GL_MULTISAMPLE_ARB );
    }

    glDrawPixels( width, height, GL_RGB, GL_UNSIGNED_BYTE, rgb );
#endif
}

class VideoWidget {
public:
    VideoWidget( IPresentationClock *clock );
    ~VideoWidget();

    GtkWidget *getWidget() {
        return _drawingArea;
    }

    IPresentationClock *getClock() {
        return _clock;
    }

    void stop();
    void play();

private:
    GdkGLConfig *_glConfig;
    GtkWidget *_drawingArea;
    IFrameSource *_source;
    int _timer;
    GMutex *_frameReadMutex;
    GCond *_frameReadCond;
    int _lastDisplayedFrame, _nextToRenderFrame;
    int _readBuffer, _writeBuffer, _filled;
    Rational _frameRate;
    guint _timeoutSourceID;
    IPresentationClock *_clock;
    int _firstFrame, _lastFrame;
    float _pixelAspectRatio;

    int64_t _presentationTime[4];
    int64_t _nextPresentationTime[4];
    Array2D<uint8_t[3]> _targets[4];
    float _rate;
    bool _quit;
    GThread *_renderThread;

    gboolean expose();
    gboolean playSingleFrame();
    gpointer playbackThread();

    static gpointer playbackThreadCallback( gpointer data ) {
        return ((VideoWidget*) data)->playbackThread();
    }

    static gboolean playSingleFrameCallback( gpointer data ) {
        return ((VideoWidget*) data)->playSingleFrame();
    }

    static gboolean exposeCallback( GtkWidget *widget, GdkEventExpose *event, gpointer data ) {
    }
};

gboolean
VideoWidget::expose() {
    GdkGLContext *glcontext = gtk_widget_get_gl_context( _drawingArea );
    GdkGLDrawable *gldrawable = gtk_widget_get_gl_drawable( _drawingArea );

    if( !gdk_gl_drawable_gl_begin( gldrawable, glcontext ) )
        return FALSE;

    static bool __glewInit = false;

    if( !__glewInit ) {
        glewInit();
        __glewInit = true;
    }

    drawFrame( &_targets[_readBuffer][0][0], 720, 480, _pixelAspectRatio );

    if( gdk_gl_drawable_is_double_buffered( gldrawable ) )
        gdk_gl_drawable_swap_buffers( gldrawable );
    else
        glFlush();

    gdk_gl_drawable_gl_end( gldrawable );

    return TRUE;
}

int64_t
getFrameTime( Rational *frameRate, int frame ) {
    return (int64_t) frame * INT64_C(1000000000) * (int64_t)(frameRate->d) / (int64_t)(frameRate->n);
}

gpointer
VideoWidget::playbackThread() {
    AVFileReader reader( "/home/james/Videos/Okra - 79b,100.avi" );
    Pulldown23RemovalFilter filter( &reader, 0, false );
    Array2D<Rgba> array( 480, 720 );

    for( ;; ) {
        int64_t startTime = _clock->getPresentationTime();
        g_mutex_lock( _frameReadMutex );
        Rational speed = _clock->getSpeed();

        while( !_quit && _filled > 2 )
            g_cond_wait( _frameReadCond, _frameReadMutex );

        if( _quit )
            return NULL;

        if( _filled < 0 )
            startTime = _clock->getPresentationTime();

        int nextFrame = _nextToRenderFrame;
        int writeBuffer = (_writeBuffer = (_writeBuffer + 1) & 3);
        g_mutex_unlock( _frameReadMutex );

//        printf( "Start rendering %d into %d...\n", nextFrame, writeBuffer );

        if( nextFrame > _lastFrame )
            nextFrame = _lastFrame;
        else if( nextFrame < _firstFrame )
            nextFrame = _firstFrame;

        filter.GetFrame( nextFrame, array );

        // Convert the results to floating-point
        for( int y = 0; y < 480; y++ ) {
            for( int x = 0; x < 720; x++ ) {
                _targets[writeBuffer][479 - y][x][0] = (uint8_t) __gamma45( array[y][x].r );
                _targets[writeBuffer][479 - y][x][1] = (uint8_t) __gamma45( array[y][x].g );
                _targets[writeBuffer][479 - y][x][2] = (uint8_t) __gamma45( array[y][x].b );
            }
        }

        //usleep( 100000 );

        _presentationTime[writeBuffer] = getFrameTime( &_frameRate, nextFrame );
        int64_t endTime = _clock->getPresentationTime();

        int64_t lastDuration = endTime - startTime;

        //printf( "Rendered frame %d into %d in %f presentation seconds...\n", _nextToRenderFrame, buffer,
        //    ((double) endTime - (double) startTime) / 1000000000.0 );
        //printf( "Presentation time %ld\n", info->_presentationTime[writeBuffer] );

        g_mutex_lock( _frameReadMutex );
        if( _filled < 0 ) {
            Rational newSpeed = _clock->getSpeed();

            if( speed.n * newSpeed.d != 0 )
                lastDuration = lastDuration * newSpeed.n * speed.d / (speed.n * newSpeed.d);
            else
                lastDuration = 0;

            speed = newSpeed;

            if( speed.n > 0 )
                _nextToRenderFrame -= 4;
            else if( speed.n < 0 )
                _nextToRenderFrame += 4;

            _filled = -1;

            // Write where the reader will read next
            _writeBuffer = _readBuffer;
        }

        _filled++;

        if( lastDuration < INT64_C(0) )
            lastDuration *= INT64_C(-1);

        if( speed.n > 0 ) {
            while( getFrameTime( &_frameRate, ++_nextToRenderFrame ) < endTime + lastDuration );
        }
        else if( speed.n < 0 ) {
            while( getFrameTime( &_frameRate, --_nextToRenderFrame ) > endTime - lastDuration );
        }

        _nextPresentationTime[writeBuffer] = getFrameTime( &_frameRate, _nextToRenderFrame );
        g_mutex_unlock( _frameReadMutex );

/*            std::stringstream filename;
        filename << "rgba" << i++ << ".exr";

        Header header( 720, 480, 40.0f / 33.0f );

        RgbaOutputFile file( filename.str().c_str(), header, WRITE_RGBA );
        file.setFrameBuffer( &array[0][0], 1, 720 );
        file.writePixels( 480 );

        puts( filename.str().c_str() );*/
    }

    return NULL;
}

gboolean
VideoWidget::playSingleFrame() {
    if( _filled > 0 ) {
        g_mutex_lock( _frameReadMutex );
        int filled = _filled;
        _readBuffer = (_readBuffer + 1) & 3;
        int64_t nextPresentationTime = _nextPresentationTime[_readBuffer];
        g_mutex_unlock( _frameReadMutex );

        if( filled != 0 ) {
            gdk_window_invalidate_rect( _drawingArea->window, &_drawingArea->allocation, FALSE );
            gdk_window_process_updates( _drawingArea->window, FALSE );

            //printf( "Painted %ld from %d...\n", info->_presentationTime[info->readBuffer], info->readBuffer );

            g_mutex_lock( _frameReadMutex );

            _filled--;

            g_cond_signal( _frameReadCond );
            g_mutex_unlock( _frameReadMutex );

            Rational speed = _clock->getSpeed();

            int timeout = (nextPresentationTime - _clock->getPresentationTime()) * speed.d / (speed.n * 1000000);
            //printf( "timeout %d\n", timeout );

            if( timeout < 0 )
                timeout = 0;

            _timeoutSourceID = g_timeout_add( timeout, playSingleFrameCallback, this );
            return FALSE;
        }
    }

    Rational speed = _clock->getSpeed();

    _timeoutSourceID = g_timeout_add(
        (1000 * _frameRate.d * speed.d) / (_frameRate.n * abs(speed.n)),
        playSingleFrameCallback, this );
    return FALSE;
}

VideoWidget::VideoWidget( IPresentationClock *clock ) {
    _glConfig = gdk_gl_config_new_by_mode ( (GdkGLConfigMode) (GDK_GL_MODE_RGB    |
                                        GDK_GL_MODE_DEPTH  |
                                        GDK_GL_MODE_DOUBLE));
    if( _glConfig == NULL )    {
        g_print( "*** Cannot find the double-buffered visual.\n" );
        g_print( "*** Trying single-buffered visual.\n" );

        /* Try single-buffered visual */
        _glConfig = gdk_gl_config_new_by_mode ((GdkGLConfigMode) (GDK_GL_MODE_RGB   |
                                        GDK_GL_MODE_DEPTH));
        if( _glConfig == NULL ) {
            g_print( "*** No appropriate OpenGL-capable visual found.\n" );
            exit( 1 );
        }
    }

    _drawingArea = gtk_drawing_area_new();
    gtk_widget_set_size_request( _drawingArea, 720, 480 );

    gtk_widget_set_gl_capability( _drawingArea,
                                _glConfig,
                                NULL,
                                TRUE,
                                GDK_GL_RGBA_TYPE );

    _clock = clock;
    _frameReadMutex = g_mutex_new();
    _frameReadCond = g_cond_new();
    _nextToRenderFrame = 5000;
    _frameRate = Rational( 24000, 1001 );
    _filled = -1;
    _readBuffer = 3;
    _writeBuffer = 3;
    _firstFrame = 0;
    _lastFrame = 6000;
    _pixelAspectRatio = 40.0f / 33.0f;
    _quit = false;
    _targets[0].resizeErase( 480, 720 );
    _targets[1].resizeErase( 480, 720 );
    _targets[2].resizeErase( 480, 720 );
    _targets[3].resizeErase( 480, 720 );

    g_object_set_data( G_OBJECT(_drawingArea), "__info", this );
    g_signal_connect( G_OBJECT(_drawingArea), "expose_event", G_CALLBACK(exposeCallback), NULL );

    g_timeout_add( 0, playSingleFrameCallback, this );
    _renderThread = g_thread_create( playbackThreadCallback, this, TRUE, NULL );
}

VideoWidget::~VideoWidget() {
    // Stop the render thread
    g_mutex_lock( _frameReadMutex );
    _quit = true;
    g_cond_signal( _frameReadCond );
    g_mutex_unlock( _frameReadMutex );

    g_thread_join( _renderThread );
}

void
VideoWidget::stop() {
    if( _timeoutSourceID != 0 ) {
        g_source_remove( _timeoutSourceID );
        _timeoutSourceID = 0;
    }

    // Just stop the production thread
    g_mutex_lock( _frameReadMutex );
    _filled = 3;
    g_mutex_unlock( _frameReadMutex );
}

void
VideoWidget::play() {
    if( _timeoutSourceID != 0 ) {
        g_source_remove( _timeoutSourceID );
        _timeoutSourceID = 0;
    }

    // Fire up the production and playback threads from scratch
    g_mutex_lock( _frameReadMutex );
    _filled = -2;
    g_cond_signal( _frameReadCond );
    g_mutex_unlock( _frameReadMutex );

    playSingleFrame();
}

gboolean
keyPressHandler( GtkWidget *widget, GdkEventKey *event, gpointer userData ) {
    VideoWidget *video = (VideoWidget*) g_object_get_data( G_OBJECT((GtkWidget*) userData), "__info" );

    Rational speed = video->getClock()->getSpeed();

    switch( event->keyval ) {
        case GDK_l:
            if( speed.n < 1 )
                speed = Rational( 1, 1 );
            else
                speed.n *= 2;
            break;

        case GDK_k:
            speed = Rational( 0, 1 );
            break;

        case GDK_j:
            if( speed.n > -1 )
                speed = Rational( -1, 1 );
            else
                speed.n *= 2;
            break;
    }

    ((SystemPresentationClock*) video->getClock())->play( speed );

    if( speed.n == 0 )
        video->stop();
    else
        video->play();

    return TRUE;
}

/*
    <source name='scene7'>
        <avsource file='Okra Principle - 7 (good take).avi' duration='5000' durationUnits='frames'>
            <stream type='video' number='0' gamma='0.45' colorspace='Rec601' />
            <stream type='audio' number='1' audioMap='stereo' />
        </avsource>
    </source>
    <clip name='shot7a/cam1/take1' source='scene7'>
        <version label='1' start='56' startUnits='frames' duration='379' durationUnits='frames'>
            <pulldown style='23' offset='3' />
        </version>
    </clip>
    <source name='scene7fostex'>
        <avsource file='scene7fostex.wav' duration='5000000' durationUnits='samples'>
            <stream type='audio' number='0' audioMap='custom'>
                <audioMap sourceChannel='left' targetChannel='center' />
            </stream>
        </avsource>
    </source>
    <clip name='shot7a/fostex/take1' source='scene7fostex'>
        <version label='1' start='5000' startUnits='ms' duration='10000' durationUnits='ms'/>
    </clip>
    <take name='scene7/shot7a/take1'>
        <version label='1'>
            <clip name='shot7a/cam1/take1' start='0' startUnits='frames' />
            <clip name='shot7a/fostex/take1' start='-56' startUnits='ms' />
        </version>
    </take>
    <timeline>
    </timeline>
*/

int
main( int argc, char *argv[] ) {
    gtk_init( &argc, &argv );
    gtk_gl_init( &argc, &argv );

    if( !g_thread_supported() )
        g_thread_init( NULL );

/*    AVFileReader reader( "/home/james/Desktop/Demo2.avi" );
    Array2D<Rgba> array( 480, 720 );
    int i = 0;

    while( reader.ReadFrame( array ) ) {
        std::stringstream filename;
        filename << "rgba" << i++ << ".exr";

        //Header header( 720, 480, 40.0f / 33.0f );

        //RgbaOutputFile file( filename.str().c_str(), header, WRITE_RGBA );
        //file.setFrameBuffer( &array[0][0], 1, 720 );
        //file.writePixels( 480 );

        puts( filename.str().c_str() );
    }*/

    GtkWidget *window = gtk_window_new( GTK_WINDOW_TOPLEVEL );
    gtk_window_set_title( GTK_WINDOW(window), "boogidy boogidy" );

    SystemPresentationClock clock;
    clock.set( Rational( 1, 1 ), 5000LL * 1000000000LL * 1001LL / 24000LL );

    VideoWidget widget( &clock );
    GtkWidget *drawingArea = widget.getWidget();

    g_signal_connect( G_OBJECT(window), "key-press-event", G_CALLBACK(keyPressHandler), drawingArea );
    g_signal_connect( G_OBJECT(window), "delete_event", G_CALLBACK(gtk_main_quit), NULL );

    gtk_container_add( GTK_CONTAINER(window), drawingArea );
    gtk_widget_show( drawingArea );

    gtk_widget_show( window );

    gtk_main();
}



