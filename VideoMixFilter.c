
#include "framework.h"

#define FOREACH_PIXEL_BEGIN(frame,pixel) \
    for( int _py = (frame)->currentDataWindow.min.y - (frame)->fullDataWindow.min.y; \
            _py <= (frame)->currentDataWindow.max.y - (frame)->fullDataWindow.min.y; _py++ ) { \
        for( int _px = (frame)->currentDataWindow.min.x - (frame)->fullDataWindow.min.x; \
            _px <= (frame)->currentDataWindow.max.x - (frame)->fullDataWindow.min.x; _px++ ) { \
            rgba_f32 *pixel = &(frame)->frameData[_py * (frame)->stride + _px]; \

#define FOREACH_PIXEL_END } }

#define frame_pixel(frame, vx, vy)    (frame)->frameData[(frame)->stride * ((vy) - (frame)->fullDataWindow.min.y) + ((vx) - (frame)->fullDataWindow.min.x)]

typedef enum {
    MIXMODE_BLEND,
    MIXMODE_ADD,
    MIXMODE_CROSSFADE
} MixMode;

static PyObject *pysourceFuncs;

typedef struct {
    PyObject_HEAD

    VideoSourceHolder srcA, srcB;
    FrameFunctionHolder mixB;
    MixMode mode;
} py_obj_VideoMixFilter;

static int
VideoMixFilter_init( py_obj_VideoMixFilter *self, PyObject *args, PyObject *kwds ) {
    static char *kwlist[] = { "srcA", "srcB", "mixB", NULL };
    PyObject *srcA, *srcB, *mixB;

    if( !PyArg_ParseTupleAndKeywords( args, kwds, "OOO", kwlist,
        &srcA, &srcB, &mixB ) )
        return -1;

    if( !takeVideoSource( srcA, &self->srcA ) )
        return -1;

    if( !takeVideoSource( srcB, &self->srcB ) )
        return -1;

    if( !takeFrameFunc( mixB, &self->mixB ) )
        return -1;

    self->mode = MIXMODE_CROSSFADE;

    return 0;
}

void mulalpha( rgba_f32_frame *frame, float f ) {
    FOREACH_PIXEL_BEGIN(frame, pixel)
        pixel->a *= f;
    FOREACH_PIXEL_END
}

void expand_frame( rgba_f32_frame *frame, box2i newWindow ) {
    int leftClear = frame->currentDataWindow.min.x - newWindow.min.x,
        rightClear = newWindow.max.x - frame->currentDataWindow.max.x;

    // Zero top rows
    for( int y = newWindow.min.y; y < frame->currentDataWindow.min.y; y++ ) {
        memset( &frame_pixel(frame, newWindow.min.x, y), 0,
            sizeof(rgba_f32) * (newWindow.max.x - newWindow.min.x + 1) );
    }

    // Zero sides
    if( leftClear > 0 || rightClear > 0 ) {
        for( int y = frame->currentDataWindow.min.y; y <= frame->currentDataWindow.max.y; y++ ) {
            if( leftClear > 0 )
                memset( &frame_pixel(frame, newWindow.min.x, y), 0,
                    sizeof(rgba_f32) * leftClear );

            if( rightClear > 0 )
                memset( &frame_pixel(frame, frame->currentDataWindow.max.x + 1, y), 0,
                    sizeof(rgba_f32) * rightClear );
        }
    }

    // Zero bottom rows
    for( int y = frame->currentDataWindow.max.y + 1; y <= newWindow.max.y; y++ ) {
        memset( &frame_pixel(frame, newWindow.min.x, y), 0,
            sizeof(rgba_f32) * (newWindow.max.x - newWindow.min.x + 1) );
    }

    // Set the frame size
    frame->currentDataWindow = newWindow;
}

static void
VideoMixFilter_getFrame32( py_obj_VideoMixFilter *self, int frameIndex, rgba_f32_frame *frame ) {
    // Gather the mix factor
    float mixB = self->mixB.constant;

    if( self->mixB.funcs ) {
        long index = frameIndex;

        self->mixB.funcs->getValues( self->mixB.source,
            1, &index, 1, &mixB );
    }

    mixB = clampf(mixB, 0.0f, 1.0f);

    if( self->mode == MIXMODE_CROSSFADE && mixB == 1.0f ) {
        // We only need frame B
        getFrame_f32( &self->srcB, frameIndex, frame );
        return;
    }

    // Gather base frame
    getFrame_f32( &self->srcA, frameIndex, frame );

    // Shortcut out if we can
    if( mixB == 0.0f )
        return;

    rgba_f32_frame tempFrame;
    v2i sizeB;

    switch( self->mode ) {
        case MIXMODE_ADD:
            // These modes don't need all of frame B
            box2i_getSize( &frame->currentDataWindow, &sizeB );
            tempFrame.fullDataWindow = frame->currentDataWindow;
            tempFrame.currentDataWindow = frame->currentDataWindow;
            break;

        case MIXMODE_BLEND:
        case MIXMODE_CROSSFADE:
            // These modes need all of frame B
            box2i_getSize( &frame->fullDataWindow, &sizeB );
            tempFrame.fullDataWindow = frame->fullDataWindow;
            tempFrame.currentDataWindow = frame->fullDataWindow;
            break;

        default:
            // Yeah... dunno.
            return;
    }

    tempFrame.frameData = slice_alloc( sizeof(rgba_f32) * sizeB.y * sizeB.x );
    tempFrame.stride = sizeB.x;

    getFrame_f32( &self->srcB, frameIndex, &tempFrame );

    // Expand them until they're the same size
    box2i newWindow = {
        {    min(frame->currentDataWindow.min.x, tempFrame.currentDataWindow.min.x),
            min(frame->currentDataWindow.min.y, tempFrame.currentDataWindow.min.y) },
        {    max(frame->currentDataWindow.max.x, tempFrame.currentDataWindow.max.x),
            max(frame->currentDataWindow.max.y, tempFrame.currentDataWindow.max.y) } };

    //expand_frame( frame, newWindow );
    //expand_frame( &tempFrame, newWindow );
    int newWidth = newWindow.max.x - newWindow.min.x + 1;

    // Perform the operation
    for( int y = newWindow.min.y; y <= newWindow.max.y; y++ ) {
        rgba_f32 *rowA = &frame_pixel(frame, newWindow.min.x, y);
        rgba_f32 *rowB = &frame_pixel(&tempFrame, newWindow.min.x, y);

        switch( self->mode ) {
            case MIXMODE_ADD:
                for( int x = 0; x < newWidth; x++ ) {
                    rowA[x].r += rowB[x].r * rowB[x].a * mixB;
                    rowA[x].g += rowB[x].g * rowB[x].a * mixB;
                    rowA[x].b += rowB[x].b * rowB[x].a * mixB;
                }
                break;

            case MIXMODE_BLEND:
                for( int x = 0; x < newWidth; x++ ) {
                    // FIXME: Gimp does something different here when
                    // the alpha of the lower layer is < 1.0f
                    rowA[x].r = rowA[x].r * (1.0f - rowB[x].a * mixB)
                        + rowB[x].r * rowB[x].a * mixB;
                    rowA[x].g = rowA[x].g * (1.0f - rowB[x].a * mixB)
                        + rowB[x].g * rowB[x].a * mixB;
                    rowA[x].b = rowA[x].b * (1.0f - rowB[x].a * mixB)
                        + rowB[x].b * rowB[x].a * mixB;
                    rowA[x].a = rowA[x].a + rowB[x].a * (1.0f - rowA[x].a) * mixB;
                }
                break;

            case MIXMODE_CROSSFADE:
                for( int x = 0; x < newWidth; x++ ) {
                    rowA[x].r = rowA[x].r * (1.0f - mixB) + rowB[x].r * mixB;
                    rowA[x].g = rowA[x].g * (1.0f - mixB) + rowB[x].g * mixB;
                    rowA[x].b = rowA[x].b * (1.0f - mixB) + rowB[x].b * mixB;
                    rowA[x].a = rowA[x].a * (1.0f - mixB) + rowB[x].a * mixB;
                }
                break;
        }
    }

    slice_free( sizeof(rgba_f32) * sizeB.y * sizeB.x, tempFrame.frameData );
}

static void
VideoMixFilter_dealloc( py_obj_VideoMixFilter *self ) {
    takeVideoSource( NULL, &self->srcA );
    takeVideoSource( NULL, &self->srcB );
    takeFrameFunc( NULL, &self->mixB );
    self->ob_type->tp_free( (PyObject*) self );
}

static VideoFrameSourceFuncs sourceFuncs = {
    0,
    NULL,
    (video_getFrame32Func) VideoMixFilter_getFrame32
};

static PyObject *
VideoMixFilter_getFuncs( py_obj_VideoMixFilter *self, void *closure ) {
    Py_INCREF(pysourceFuncs);
    return pysourceFuncs;
}

static PyGetSetDef VideoMixFilter_getsetters[] = {
    { "_videoFrameSourceFuncs", (getter) VideoMixFilter_getFuncs, NULL, "Video frame source C API." },
    { NULL }
};

static PyTypeObject py_type_VideoMixFilter = {
    PyObject_HEAD_INIT(NULL)
    0,            // ob_size
    "fluggo.media.VideoMixFilter",    // tp_name
    sizeof(py_obj_VideoMixFilter),    // tp_basicsize
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_dealloc = (destructor) VideoMixFilter_dealloc,
    .tp_init = (initproc) VideoMixFilter_init,
    .tp_getset = VideoMixFilter_getsetters
};

void init_VideoMixFilter( PyObject *module ) {
    if( PyType_Ready( &py_type_VideoMixFilter ) < 0 )
        return;

    Py_INCREF( (PyObject*) &py_type_VideoMixFilter );
    PyModule_AddObject( module, "VideoMixFilter", (PyObject *) &py_type_VideoMixFilter );

    pysourceFuncs = PyCObject_FromVoidPtr( &sourceFuncs, NULL );
}



