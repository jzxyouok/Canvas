/*
    This file is part of the Fluggo Media Library for high-quality
    video and audio processing.

    Copyright 2009 Brian J. Crowell <brian@fluggo.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "pyframework.h"

static PyObject *pysourceFuncs;

typedef struct {
    PyObject_HEAD

    AudioSourceHolder source;
} py_obj_AudioPassThroughFilter;

static int
AudioPassThroughFilter_init( py_obj_AudioPassThroughFilter *self, PyObject *args, PyObject *kwds ) {
    PyObject *source;

    if( !PyArg_ParseTuple( args, "O", &source ) )
        return -1;

    if( !py_audio_take_source( source, &self->source ) )
        return -1;

    return 0;
}

static void
AudioPassThroughFilter_getFrame( py_obj_AudioPassThroughFilter *self, audio_frame *frame ) {
    if( !self->source.source.funcs ) {
        // No result
        frame->current_max_sample = frame->current_min_sample - 1;
        return;
    }

    self->source.source.funcs->getFrame( self->source.source.obj, frame );
}

static void
AudioPassThroughFilter_dealloc( py_obj_AudioPassThroughFilter *self ) {
    py_audio_take_source( NULL, &self->source );
    Py_TYPE(self)->tp_free( (PyObject*) self );
}

static PyObject *
AudioPassThroughFilter_getSource( py_obj_AudioPassThroughFilter *self ) {
    if( self->source.source.obj == NULL )
        Py_RETURN_NONE;

    PyObject *obj = (PyObject *) self->source.source.obj;
    Py_INCREF(obj);
    return obj;
}

static PyObject *
AudioPassThroughFilter_setSource( py_obj_AudioPassThroughFilter *self, PyObject *args, void *closure ) {
    PyObject *source;

    if( !PyArg_ParseTuple( args, "O", &source ) )
        return NULL;

    if( !py_audio_take_source( source, &self->source ) )
        return NULL;

    Py_RETURN_NONE;
}

static AudioFrameSourceFuncs sourceFuncs = {
    .getFrame = (audio_getFrameFunc) AudioPassThroughFilter_getFrame
};

static PyObject *
AudioPassThroughFilter_getFuncs( py_obj_AudioPassThroughFilter *self, void *closure ) {
    Py_INCREF(pysourceFuncs);
    return pysourceFuncs;
}

static PyGetSetDef AudioPassThroughFilter_getsetters[] = {
    { AUDIO_FRAME_SOURCE_FUNCS, (getter) AudioPassThroughFilter_getFuncs, NULL, "Audio frame source C API." },
    { NULL }
};

static PyMethodDef AudioPassThroughFilter_methods[] = {
    { "source", (PyCFunction) AudioPassThroughFilter_getSource, METH_NOARGS,
        "Gets the audio source." },
    { "set_source", (PyCFunction) AudioPassThroughFilter_setSource, METH_VARARGS,
        "Sets the audio source." },
    { NULL }
};

static PyTypeObject py_type_AudioPassThroughFilter = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "fluggo.media.process.AudioPassThroughFilter",
    .tp_basicsize = sizeof(py_obj_AudioPassThroughFilter),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .tp_base = &py_type_AudioSource,
    .tp_new = PyType_GenericNew,
    .tp_dealloc = (destructor) AudioPassThroughFilter_dealloc,
    .tp_init = (initproc) AudioPassThroughFilter_init,
    .tp_getset = AudioPassThroughFilter_getsetters,
    .tp_methods = AudioPassThroughFilter_methods,
};

void init_AudioPassThroughFilter( PyObject *module ) {
    if( PyType_Ready( &py_type_AudioPassThroughFilter ) < 0 )
        return;

    Py_INCREF( (PyObject*) &py_type_AudioPassThroughFilter );
    PyModule_AddObject( module, "AudioPassThroughFilter", (PyObject *) &py_type_AudioPassThroughFilter );

    pysourceFuncs = PyCapsule_New( &sourceFuncs, AUDIO_FRAME_SOURCE_FUNCS, NULL );
}
